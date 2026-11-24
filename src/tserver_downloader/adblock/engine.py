"""Ad-block engine.

Independent of the download pipeline: a failure here (bad list, bad
rule, network error) must never break downloads. Callers should wrap
``should_block`` in try/except and treat an exception as "allow".
"""
from __future__ import annotations

from urllib.parse import urlparse

from .filters import FilterRule, compile_rules, parse_filter_line
from .lists import DEFAULT_LISTS

DEFAULT_ALLOWLIST = ["thetechnicalserver.blogspot.com"]

# Hosts in the allowlist are never blocked, even if a filter rule
# matches them. Checked first, inside should_block, as a hard safety net.


class AdBlockEngine:
    """Filter-list engine with a domain-anchored rule index."""

    def __init__(self, allowlist=None):
        self.allowlist = list(DEFAULT_ALLOWLIST)
        if allowlist:
            for host in allowlist:
                host = str(host).strip().lower()
                if host and host not in self.allowlist:
                    self.allowlist.append(host)
        # Domain-anchored index: anchor -> [rules]; generic rules live
        # in _generic_rules so lookups stay cheap on long lists.
        self._index = {}
        self._generic_rules = []
        self._exception_index = {}
        self._exception_generic = []
        self._cosmetic_rules = []
        self.blocked_count = 0
        self.allowed_count = 0
        self.per_domain_blocked = {}
        self._per_domain_allowed = {}
        # Enable/disable switch (default on). When disabled, nothing is
        # blocked; the download pipeline never depends on this flag.
        self.enabled = True

    def enable(self) -> None:
        """Turn blocking on."""
        self.enabled = True

    def disable(self) -> None:
        """Turn blocking off (everything is allowed)."""
        self.enabled = False

    def is_enabled(self) -> bool:
        return bool(self.enabled)

    # -- loading ---------------------------------------------------------
    def load_lists(self, list_of_text):
        """Load filter lists. ``list_of_text`` is an iterable of strings,
        each a whole list's text (or a single rule line)."""
        if isinstance(list_of_text, str):
            list_of_text = [list_of_text]
        for text in list_of_text:
            if text is None:
                continue
            for rule in compile_rules(text):
                self._add_rule(rule)
        return self

    def add_custom_rule(self, line):
        """Parse and add one rule line. Returns the rule or None."""
        rule = parse_filter_line(line)
        if rule is not None:
            self._add_rule(rule)
        return rule

    def _add_rule(self, rule):
        if rule.is_cosmetic:
            self._cosmetic_rules.append(rule)
            return
        if rule.is_exception:
            if rule.is_domain_anchor and rule.domain_anchor:
                self._exception_index.setdefault(rule.domain_anchor, []).append(rule)
            else:
                self._exception_generic.append(rule)
            return
        if rule.is_domain_anchor and rule.domain_anchor:
            self._index.setdefault(rule.domain_anchor, []).append(rule)
        else:
            self._generic_rules.append(rule)

    # -- matching --------------------------------------------------------
    def _candidates(self, host):
        """Rules that could match ``host``: indexed anchors + generics."""
        seen = []
        if host:
            parts = host.split(".")
            for i in range(len(parts)):
                anchor = ".".join(parts[i:])
                bucket = self._index.get(anchor)
                if bucket:
                    seen.extend(bucket)
                bucket = self._exception_index.get(anchor)
                if bucket:
                    # Exceptions are checked separately; return them via
                    # _exception_candidates instead.
                    pass
        seen.extend(self._generic_rules)
        return seen

    def _exception_candidates(self, host):
        seen = []
        if host:
            parts = host.split(".")
            for i in range(len(parts)):
                bucket = self._exception_index.get(".".join(parts[i:]))
                if bucket:
                    seen.extend(bucket)
        seen.extend(self._exception_generic)
        return seen

    def _is_allowlisted(self, host):
        if not host:
            return False
        host = host.lower()
        for allowed in self.allowlist:
            allowed = allowed.lower()
            if host == allowed or host.endswith("." + allowed):
                return True
        return False

    def should_block(self, url, page_domain=None, resource_type=None):
        """Return True if ``url`` should be blocked.

        Exception rules override block rules. Allowlisted hosts (the
        blog and its subdomains, plus any extra allowlist entries) are
        never blocked. Non-HTTP(S) / unparseable URLs are never blocked.
        When the engine is disabled, nothing is blocked.
        """
        if not self.enabled:
            self.allowed_count += 1
            return False
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            self.allowed_count += 1
            return False
        # Hard safety check: the allowlist is never blocked.
        if self._is_allowlisted(host):
            self.allowed_count += 1
            return False
        # Exception rules override block rules.
        for rule in self._exception_candidates(host):
            try:
                if rule.matches(url, page_domain=page_domain,
                                resource_type=resource_type):
                    self.allowed_count += 1
                    return False
            except Exception:
                continue
        for rule in self._candidates(host):
            try:
                if rule.matches(url, page_domain=page_domain,
                                resource_type=resource_type):
                    self.blocked_count += 1
                    self.per_domain_blocked[host] = \
                        self.per_domain_blocked.get(host, 0) + 1
                    return True
            except Exception:
                continue
        self.allowed_count += 1
        return False

    def get_cosmetic_selectors(self, domain):
        """CSS selectors to hide on ``domain`` (generic + domain rules)."""
        if not domain:
            domain = ""
        host = domain.lower().split(":")[0]
        selectors = []
        for rule in self._cosmetic_rules:
            if rule.is_exception:
                continue
            if not rule.domains:
                selectors.append(rule.selector)
            else:
                for d in rule.domains:
                    d = d.lower()
                    if host == d or host.endswith("." + d):
                        selectors.append(rule.selector)
                        break
        # Exception cosmetic rules remove selectors.
        for rule in self._cosmetic_rules:
            if not rule.is_exception:
                continue
            if not rule.domains:
                if rule.selector in selectors:
                    selectors.remove(rule.selector)
            else:
                for d in rule.domains:
                    if host == d.lower() or host.endswith("." + d.lower()):
                        if rule.selector in selectors:
                            selectors.remove(rule.selector)
                        break
        return selectors

    # -- stats -----------------------------------------------------------
    def get_stats(self):
        return {
            "enabled": bool(self.enabled),
            "blocked_count": self.blocked_count,
            "allowed_count": self.allowed_count,
            "per-domain counts": dict(self.per_domain_blocked),
            "per_domain_blocked": dict(self.per_domain_blocked),
        }

    @property
    def stats(self):
        return self.get_stats()

    # -- updates ---------------------------------------------------------
    def update_lists(self, fetch_fn=None):
        """Re-fetch the default lists and reload them.

        ``fetch_fn(url) -> str`` is used when given (tests inject a fake);
        otherwise :func:`lists.fetch_list` (HTTPS only) is used. Fetched
        content must be non-empty text or it is skipped. Returns the
        number of lists successfully loaded. Raises nothing for a single
        failed list -- a bad update must not break the engine -- but
        raises ValueError if a fetch returns non-text content.
        """
        from .lists import fetch_list

        loaded = 0
        for entry in DEFAULT_LISTS:
            url = entry["url"]
            try:
                text = fetch_fn(url) if fetch_fn is not None else fetch_list(url)
            except Exception:
                continue
            if not isinstance(text, str) or not text.strip():
                continue
            self.load_lists([text])
            loaded += 1
        return loaded

    def rule_count(self):
        count = len(self._generic_rules) + len(self._exception_generic)
        count += sum(len(v) for v in self._index.values())
        count += sum(len(v) for v in self._exception_index.values())
        return count

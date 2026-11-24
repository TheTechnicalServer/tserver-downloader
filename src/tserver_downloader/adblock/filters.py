"""EasyList-syntax filter parser.

Parses Adblock Plus / EasyList filter lines into :class:`FilterRule`
objects. Network rules, exception rules (``@@``), cosmetic rules
(``##`` / ``#@#``) and ``$`` options (``domain=``, ``third-party``,
``script`` / ``image`` / ``xmlhttprequest`` and friends) are supported.
Comments (``!`` and ``[Adblock Plus ...]`` headers) and blank lines
produce ``None``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

# Resource types recognised in $ options. Aliases map to canonical names.
_RESOURCE_ALIASES = {
    "script": "script",
    "image": "image",
    "stylesheet": "stylesheet",
    "object": "object",
    "xmlhttprequest": "xmlhttprequest",
    "xhr": "xmlhttprequest",
    "subdocument": "subdocument",
    "document": "document",
    "font": "font",
    "media": "media",
    "websocket": "websocket",
    "other": "other",
    "ping": "ping",
}

_SEPARATOR_RE = re.compile(r"[^a-zA-Z0-9_\-%\.]")


@dataclass
class FilterRule:
    """One parsed filter rule."""

    pattern: str = ""
    raw: str = ""
    is_exception: bool = False
    # Domains the rule is restricted to (from $domain= or the part before
    # ## / #@#). Empty means the rule applies to every domain.
    domains: list = field(default_factory=list)
    # Domains the rule must NOT apply to (``~``-prefixed entries).
    excluded_domains: list = field(default_factory=list)
    is_cosmetic: bool = False
    selector: str = ""
    # Resource types the rule applies to, or None for all types.
    resource_types: set | None = None
    # True for ||domain^ style anchors.
    is_domain_anchor: bool = False
    domain_anchor: str = ""
    # True = third-party only, False = first-party only, None = either.
    third_party: bool | None = None
    _regex: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.pattern:
            self._regex = _compile_pattern(self.pattern, self.is_domain_anchor,
                                           self.domain_anchor)

    def _domain_allowed(self, page_domain):
        """Check $domain= / cosmetic-domain restrictions."""
        if page_domain:
            host = page_domain.lower().split(":")[0]
            if self.excluded_domains and _domain_in(host, self.excluded_domains):
                return False
            if self.domains and not _domain_in(host, self.domains):
                return False
        elif self.domains:
            # No page domain known: a domain-restricted rule cannot be
            # proven to apply. Cosmetic rules with no domain are generic.
            return False
        return True

    def matches(self, url, page_domain=None, resource_type=None):
        """Return True if this network rule matches ``url``."""
        if self.is_cosmetic or not self.pattern and not self.is_domain_anchor:
            return False
        if self.resource_types is not None and resource_type is not None:
            if resource_type.lower() not in self.resource_types:
                return False
        if not self._domain_allowed(page_domain):
            return False
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            host = ""
        if self.third_party is True and page_domain:
            if _domain_in(host, [page_domain.lower()]):
                return False
        if self.third_party is False and page_domain:
            if not _domain_in(host, [page_domain.lower()]):
                return False
        if self.is_domain_anchor:
            if not host:
                return False
            return host == self.domain_anchor or host.endswith("." + self.domain_anchor)
        if self._regex is not None:
            return self._regex.search(url) is not None
        return self.pattern in url


@dataclass
class FilterList:
    """A named collection of parsed rules."""

    name: str = ""
    url: str = ""
    rules: list = field(default_factory=list)

    def __len__(self):
        return len(self.rules)


def _domain_in(host, domains):
    host = host.lower()
    for d in domains:
        d = d.lower()
        if host == d or host.endswith("." + d):
            return True
    return False


def _compile_pattern(pattern, is_domain_anchor=False, domain_anchor=""):
    """Compile an EasyList pattern to a regex (or None for plain text)."""
    if is_domain_anchor:
        return None
    pat = pattern
    anchored_start = False
    anchored_end = False
    if pat.startswith("|"):
        anchored_start = True
        pat = pat[1:]
    if pat.endswith("|"):
        anchored_end = True
        pat = pat[:-1]
    # Build regex: escape everything, then restore wildcards/separators.
    out = []
    i = 0
    while i < len(pat):
        ch = pat[i]
        if ch == "*":
            out.append(".*")
        elif ch == "^":
            out.append(r"(?:[^a-zA-Z0-9_\-%\.]|$)")
        else:
            out.append(re.escape(ch))
        i += 1
    regex = "".join(out)
    if anchored_start:
        regex = "^" + regex
    if anchored_end:
        regex = regex + "$"
    try:
        return re.compile(regex)
    except re.error:
        return None


def parse_filter_line(line):
    """Parse one EasyList line. Returns ``FilterRule`` or ``None``.

    ``None`` is returned for blank lines, comments (``!``), section
    headers (``[Adblock Plus ...]``) and unparseable lines.
    """
    if line is None:
        return None
    raw = line.strip()
    if not raw:
        return None
    if raw.startswith("!") or raw.startswith("[Adblock Plus"):
        return None

    # Cosmetic exception: domain#@#selector
    if "#@#" in raw:
        left, selector = raw.split("#@#", 1)
        selector = selector.strip()
        if not selector:
            return None
        domains = [d.strip() for d in left.split(",") if d.strip()] if left else []
        return FilterRule(pattern="", raw=raw, is_exception=True,
                          domains=domains, is_cosmetic=True, selector=selector)
    # Cosmetic: domain##selector (must be checked before network parsing;
    # ## never appears in a network pattern)
    if "##" in raw:
        left, selector = raw.split("##", 1)
        selector = selector.strip()
        if not selector:
            return None
        domains = [d.strip() for d in left.split(",") if d.strip()] if left else []
        return FilterRule(pattern="", raw=raw, is_exception=False,
                          domains=domains, is_cosmetic=True, selector=selector)

    is_exception = False
    rest = raw
    if rest.startswith("@@"):
        is_exception = True
        rest = rest[2:]

    # Split $ options.
    pattern_part, sep, options_part = rest.partition("$")
    pattern = pattern_part.strip()
    if not pattern and not sep:
        return None

    domains = []
    excluded_domains = []
    resource_types = None
    third_party = None

    if sep:
        for opt in options_part.split(","):
            opt = opt.strip()
            if not opt:
                continue
            if opt == "third-party":
                third_party = True
            elif opt == "first-party" or opt == "~third-party":
                third_party = False
            elif opt.startswith("domain="):
                for d in opt[len("domain="):].split("|"):
                    d = d.strip()
                    if not d:
                        continue
                    if d.startswith("~"):
                        excluded_domains.append(d[1:])
                    else:
                        domains.append(d)
            elif opt.startswith("~") and opt[1:] in _RESOURCE_ALIASES:
                # Negated resource type: apply to all except this one.
                # Represented by leaving resource_types None and recording
                # the exclusion via a marker set is not needed for the
                # engine's common paths; treat as unrestricted.
                continue
            elif opt in _RESOURCE_ALIASES:
                if resource_types is None:
                    resource_types = set()
                resource_types.add(_RESOURCE_ALIASES[opt])
            # Unknown options (e.g. $popup, $match-case) are ignored:
            # the rule still applies.

    is_domain_anchor = False
    domain_anchor = ""
    if pattern.startswith("||"):
        is_domain_anchor = True
        anchor = pattern[2:]
        # Anchor ends at the first ^, /, or *.
        m = re.split(r"[\^/*]", anchor, maxsplit=1)
        domain_anchor = m[0].lower().rstrip(".")
        if not domain_anchor:
            return None

    if not pattern and not is_domain_anchor:
        return None

    return FilterRule(
        pattern=pattern,
        raw=raw,
        is_exception=is_exception,
        domains=domains,
        excluded_domains=excluded_domains,
        is_cosmetic=False,
        selector="",
        resource_types=resource_types,
        is_domain_anchor=is_domain_anchor,
        domain_anchor=domain_anchor,
        third_party=third_party,
    )


def compile_rules(source):
    """Compile filter text (or a list of lines) into a list of FilterRule.

    Accepts a single string (one rule per line), a list of strings, or a
    list of already-parsed FilterRule objects (returned unchanged).
    """
    if isinstance(source, str):
        lines = source.splitlines()
    else:
        lines = list(source)
    rules = []
    for item in lines:
        if isinstance(item, FilterRule):
            rules.append(item)
            continue
        rule = parse_filter_line(item)
        if rule is not None:
            rules.append(rule)
    return rules

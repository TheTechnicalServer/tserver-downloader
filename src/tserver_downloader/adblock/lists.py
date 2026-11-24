"""Default filter lists and bundled offline rules."""
from __future__ import annotations

import requests

DEFAULT_LISTS = [
    {"name": "EasyList", "url": "https://easylist.to/easylist/easylist.txt"},
    {"name": "EasyPrivacy", "url": "https://easylist.to/easylist/easyprivacy.txt"},
]

# Small bundled sample so the engine works offline. All are real
# EasyList-style patterns for well-known ad/tracking domains.
BUNDLED_RULES = [
    "||doubleclick.net^",
    "||googlesyndication.com^",
    "||googleadservices.com^",
    "||googletagmanager.com^",
    "||googletagservices.com^",
    "||google-analytics.com^",
    "||adservice.google.com^",
    "||ads.yahoo.com^",
    "||advertising.com^",
    "||adnxs.com^",
    "||adsrvr.org^",
    "||amazon-adsystem.com^",
    "||criteo.com^",
    "||criteo.net^",
    "||moatads.com^",
    "||scorecardresearch.com^",
    "||facebook.com/tr/*",
    "||analytics.twitter.com^",
    "||hotjar.com^",
    "||mixpanel.com^",
    "||segment.io^",
    "||intercom.io^",
    "||taboola.com^",
    "||outbrain.com^",
    "||adsafeprotected.com^",
    "||pubmatic.com^",
    "||rubiconproject.com^",
    "||openx.net^",
    "||smartadserver.com^",
    "||zedo.com^",
    "||adform.net^",
    "||adroll.com^",
    "||quantserve.com^",
    "||nr-data.net^",
    "||newrelic.com^",
    "||fullstory.com^",
    "||doubleclick.net^$third-party",
    "||pagead2.googlesyndication.com^",
    "||tpc.googlesyndication.com^",
    "@@||example.com^$document",
    "example.com##.ad-banner",
    "thetechnicalserver.blogspot.com##.sidebar-ads",
]

BUNDLED_LIST_TEXT = "\n".join(BUNDLED_RULES) + "\n"


def fetch_list(url, timeout=15):
    """Fetch one filter list over HTTPS only.

    Raises ValueError for non-HTTPS URLs or for empty / non-text content,
    and propagates requests errors for network/HTTP failures.
    """
    if not isinstance(url, str) or not url.lower().startswith("https://"):
        raise ValueError("filter lists may only be fetched over HTTPS: %r" % (url,))
    response = requests.get(url, timeout=timeout,
                            headers={"User-Agent": "TServer-Downloader/1.0.0"})
    response.raise_for_status()
    content_type = (response.headers.get("Content-Type") or "").lower()
    if content_type and "text" not in content_type and "plain" not in content_type \
            and "octet-stream" not in content_type:
        raise ValueError("filter list did not return text content: %s" % content_type)
    text = response.text
    if not text or not text.strip():
        raise ValueError("filter list returned empty content: %s" % url)
    return text


def load_bundled_lists():
    """Return the bundled offline rule lines (at least 20 real patterns)."""
    return list(BUNDLED_RULES)

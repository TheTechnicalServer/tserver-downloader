"""Ad-block / privacy engine (independent of the download pipeline)."""
from .engine import DEFAULT_ALLOWLIST, AdBlockEngine
from .filters import FilterList, FilterRule, compile_rules, parse_filter_line
from .lists import BUNDLED_RULES, DEFAULT_LISTS, fetch_list, load_bundled_lists

__all__ = [
    "AdBlockEngine",
    "FilterList",
    "FilterRule",
    "compile_rules",
    "parse_filter_line",
    "DEFAULT_ALLOWLIST",
    "DEFAULT_LISTS",
    "BUNDLED_RULES",
    "fetch_list",
    "load_bundled_lists",
]

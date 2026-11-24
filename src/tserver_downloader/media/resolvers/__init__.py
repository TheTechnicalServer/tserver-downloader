"""Site-specific resolvers for TServer Downloader.

OWN architecture, written from scratch. No IDM code is used or referenced.

Each resolver is isolated: a faulty resolver returns ``[]`` instead of
raising, and resolvers never share state. Registration and lookup delegate
to :class:`~..resolver.SiteResolverRegistry`; unmatched URLs fall back to
the generic resolver (direct files + open HLS/DASH manifests only).
"""

from __future__ import annotations

from ..resolver import SiteResolverRegistry, get_resolver, register_resolver
from .generic import GenericResolver, resolve as generic_resolve
from .youtube import PermittedYouTubeResolver

__all__ = [
    "GenericResolver",
    "PermittedYouTubeResolver",
    "SiteResolverRegistry",
    "register_resolver",
    "get_resolver",
    "list_resolvers",
    "resolve_with_registry",
]


def list_resolvers() -> list[str]:
    """Names of the site-specific resolvers currently registered."""
    return SiteResolverRegistry.list_resolvers()


def resolve_with_registry(url: str, html: str | None = None) -> list:
    """Resolve ``url`` through the matching registered resolver.

    Returns ``[]`` on any failure, including a crashing resolver.
    """
    return SiteResolverRegistry.resolve(url, html=html)

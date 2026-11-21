"""Provider registry mapping PackageName -> official upstream resolver."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ..models import Arch, PackageName, UpstreamRelease
from .base import HttpClient
from .cli import fetch_cli_release
from .hub import fetch_hub_release
from .ide import fetch_ide_release

ProviderFn = Callable[[HttpClient, Arch], UpstreamRelease]

_PROVIDERS: dict[PackageName, ProviderFn] = {
    PackageName.ANTIGRAVITY: fetch_hub_release,
    PackageName.ANTIGRAVITY_CLI: fetch_cli_release,
    PackageName.ANTIGRAVITY_IDE: fetch_ide_release,
}


def fetch_upstream_release(
    client: HttpClient,
    package: PackageName,
    arch: Arch,
) -> UpstreamRelease:
    """Resolve latest official release for a single package and architecture."""
    return _PROVIDERS[package](client, arch)


def fetch_all_upstream(
    client: HttpClient,
    packages: Sequence[PackageName],
    arch: Arch,
) -> dict[PackageName, UpstreamRelease]:
    """Resolve latest official releases for all requested packages."""
    return {pkg: fetch_upstream_release(client, pkg, arch) for pkg in packages}

"""Upstream release provider for Google Antigravity CLI (agy)."""

from __future__ import annotations

import re

from ..models import Arch, ChecksumKind, PackageName, ProviderError, UpstreamRelease
from .base import HttpClient

CLI_MANIFEST_BASE = (
    "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests"
)

_ARCH_MANIFEST: dict[Arch, str] = {
    Arch.X86_64: "linux_amd64.json",
    Arch.AARCH64: "linux_arm64.json",
}

_CLI_URL_RE = re.compile(r"/antigravity-cli/([0-9]+(?:\.[0-9]+)+)-([0-9]+)/")


def fetch_cli_release(client: HttpClient, arch: Arch) -> UpstreamRelease:
    """Resolve latest Antigravity CLI release and SHA-512 digest."""
    manifest_url = f"{CLI_MANIFEST_BASE}/{_ARCH_MANIFEST[arch]}"
    payload = client.get_json(manifest_url)
    if not isinstance(payload, dict):
        raise ProviderError(f"Malformed CLI manifest from {manifest_url}: {payload!r}")

    version = str(payload.get("version", "")).strip()
    download_url = str(payload.get("url", "")).strip()
    sha512 = str(payload.get("sha512", "")).strip().lower()

    if not version or not download_url or not sha512:
        raise ProviderError(f"Incomplete CLI manifest from {manifest_url}: {payload!r}")

    match = _CLI_URL_RE.search(download_url)
    if not match:
        raise ProviderError(
            f"Could not parse version/build_id from Antigravity CLI URL: {download_url}"
        )

    url_version, build_id = match.group(1), match.group(2)
    if url_version != version:
        version = url_version

    # Match AUR antigravity-cli pkgver convention: <semver>_<build_id>
    pkgver = f"{version}_{build_id}"

    return UpstreamRelease(
        package=PackageName.ANTIGRAVITY_CLI,
        arch=arch,
        version=version,
        build_id=build_id,
        pkgver=pkgver,
        download_url=download_url,
        checksum_kind=ChecksumKind.SHA512,
        checksum_value=sha512,
        source_endpoint=manifest_url,
    )

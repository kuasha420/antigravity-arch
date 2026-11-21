"""Upstream release provider for Google Antigravity IDE."""

from __future__ import annotations

import re
import urllib.parse

from ..models import Arch, ChecksumKind, PackageName, ProviderError, UpstreamRelease
from .base import HttpClient

IDE_UPDATE_BASE = (
    "https://antigravity-ide-auto-updater-974169037036.us-central1.run.app/api/update"
)

_ARCH_SEGMENT: dict[Arch, str] = {
    Arch.X86_64: "linux-x64",
    Arch.AARCH64: "linux-arm64",
}

_IDE_URL_RE = re.compile(r"/stable/([0-9]+(?:\.[0-9]+)+)-([0-9]+)/")


def _encode_url_spaces(raw_url: str) -> str:
    parsed = urllib.parse.urlsplit(raw_url)
    encoded_path = urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/:@!$&'()*+,;=-._~")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, encoded_path, parsed.query, parsed.fragment)
    )


def fetch_ide_release(client: HttpClient, arch: Arch) -> UpstreamRelease:
    """Resolve latest Antigravity IDE release and SHA-256 digest."""
    endpoint = f"{IDE_UPDATE_BASE}/{_ARCH_SEGMENT[arch]}/stable/latest"
    payload = client.get_json(endpoint)
    if not isinstance(payload, dict):
        raise ProviderError(f"Malformed IDE update payload from {endpoint}: {payload!r}")

    raw_url = str(payload.get("url", "")).strip()
    sha256 = str(payload.get("sha256hash", "")).strip().lower()
    if not raw_url or not sha256:
        raise ProviderError(f"Incomplete IDE update payload from {endpoint}: {payload!r}")

    match = _IDE_URL_RE.search(raw_url)
    if not match:
        raise ProviderError(f"Could not parse IDE version/build_id from URL: {raw_url}")

    version, build_id = match.group(1), match.group(2)
    encoded_url = _encode_url_spaces(raw_url)
    ts = payload.get("timestamp")
    raw_ts = str(ts) if ts is not None else None

    return UpstreamRelease(
        package=PackageName.ANTIGRAVITY_IDE,
        arch=arch,
        version=version,
        build_id=build_id,
        pkgver=version,
        download_url=encoded_url,
        checksum_kind=ChecksumKind.SHA256,
        checksum_value=sha256,
        source_endpoint=endpoint,
        raw_timestamp=raw_ts,
    )

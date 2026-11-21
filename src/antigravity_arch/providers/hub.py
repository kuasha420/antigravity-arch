"""Upstream release provider for Google Antigravity (Hub / Desktop App)."""

from __future__ import annotations

from ..integrity import parse_gcs_crc32c_headers
from ..models import Arch, ChecksumKind, PackageName, ProviderError, UpstreamRelease
from .base import HttpClient

HUB_RELEASES_URL = "https://antigravity-hub-auto-updater-974169037036.us-central1.run.app/releases"
HUB_ARTIFACT_BASE = "https://storage.googleapis.com/antigravity-public/antigravity-hub"

_ARCH_SEGMENT: dict[Arch, str] = {
    Arch.X86_64: "linux-x64",
    Arch.AARCH64: "linux-arm",
}


def fetch_hub_release(client: HttpClient, arch: Arch) -> UpstreamRelease:
    """Resolve latest Antigravity Desktop release and GCS CRC32C hash."""
    payload = client.get_json(HUB_RELEASES_URL)
    if not isinstance(payload, list) or not payload:
        raise ProviderError(f"Unexpected empty releases payload from {HUB_RELEASES_URL}")

    latest = payload[0]
    if not isinstance(latest, dict):
        raise ProviderError(f"Malformed release entry from {HUB_RELEASES_URL}: {latest!r}")

    version = str(latest.get("version", "")).strip()
    execution_id = str(latest.get("execution_id", "")).strip()
    if not version or not execution_id:
        raise ProviderError(
            f"Missing version or execution_id in Antigravity Hub release: {latest!r}"
        )

    arch_seg = _ARCH_SEGMENT[arch]
    download_url = f"{HUB_ARTIFACT_BASE}/{version}-{execution_id}/{arch_seg}/Antigravity.tar.gz"

    head_resp = client.head(download_url)
    crc32c_b64 = parse_gcs_crc32c_headers(head_resp.get_headers("x-goog-hash"))
    if not crc32c_b64:
        raise ProviderError(
            f"Missing x-goog-hash crc32c header on Antigravity artifact HEAD: {download_url}"
        )

    raw_len = head_resp.get_header("content-length")
    content_length = int(raw_len) if raw_len and raw_len.isdigit() else None

    return UpstreamRelease(
        package=PackageName.ANTIGRAVITY,
        arch=arch,
        version=version,
        build_id=execution_id,
        pkgver=version,
        download_url=download_url,
        checksum_kind=ChecksumKind.CRC32C_B64,
        checksum_value=crc32c_b64,
        content_length=content_length,
        source_endpoint=HUB_RELEASES_URL,
        raw_timestamp=head_resp.get_header("last-modified"),
    )

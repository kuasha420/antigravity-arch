"""Hermetic tests for Cloud Run upstream providers and 3-tier AUR recipe resolution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from antigravity_arch.models import Arch, ChecksumKind, PackageName, ProviderError, RecipeOrigin
from antigravity_arch.providers.aur import fetch_aur_info, prepare_recipe_workspace
from antigravity_arch.providers.base import HttpClient, HttpResponse
from antigravity_arch.providers.cli import fetch_cli_release
from antigravity_arch.providers.hub import fetch_hub_release
from antigravity_arch.providers.ide import fetch_ide_release


class FakeHttpClient(HttpClient):
    """In-memory mock HTTP client with zero network I/O."""

    def __init__(self, responses: dict[tuple[str, str], HttpResponse | Exception]) -> None:
        super().__init__(timeout=5, max_retries=0, sleep_fn=lambda _: None)
        self._responses = responses

    def request(self, url: str, *, method: str = "GET") -> HttpResponse:
        item = self._responses.get((method, url))
        if item is None:
            raise ProviderError(f"Unmocked request: {method} {url}")
        if isinstance(item, Exception):
            raise item
        return item


def _json_resp(url: str, data: Any) -> HttpResponse:
    return HttpResponse(
        url=url,
        status=200,
        headers={"content-type": ["application/json"]},
        body=json.dumps(data).encode("utf-8"),
    )


@pytest.mark.parametrize(
    ("arch", "arch_seg", "crc"),
    [
        (Arch.X86_64, "linux-x64", "BqScAQ=="),
        (Arch.AARCH64, "linux-arm", "OOlQ1g=="),
    ],
)
def test_fetch_hub_release(arch: Arch, arch_seg: str, crc: str) -> None:
    rel_url = "https://antigravity-hub-auto-updater-974169037036.us-central1.run.app/releases"
    tar_url = (
        f"https://storage.googleapis.com/antigravity-public/antigravity-hub/"
        f"2.34.0-5887025308631040/{arch_seg}/Antigravity.tar.gz"
    )
    client = FakeHttpClient(
        {
            ("GET", rel_url): _json_resp(
                rel_url,
                [
                    {"version": "2.34.0", "execution_id": "5887025308631040"},
                    {"version": "2.33.0", "execution_id": "4731201358004224"},
                ],
            ),
            ("HEAD", tar_url): HttpResponse(
                url=tar_url,
                status=200,
                headers={
                    "x-goog-hash": [f"crc32c={crc}"],
                    "content-length": ["211201084"],
                    "last-modified": ["Fri, 20 Nov 2026 21:07:16 GMT"],
                },
                body=b"",
            ),
        }
    )

    rel = fetch_hub_release(client, arch)
    assert rel.package is PackageName.ANTIGRAVITY
    assert rel.pkgver == "2.34.0"
    assert rel.build_id == "5887025308631040"
    assert rel.checksum_kind is ChecksumKind.CRC32C_B64
    assert rel.checksum_value == crc
    assert rel.content_length == 211201084


@pytest.mark.parametrize(
    ("arch", "manifest_name", "url_seg", "tar_name"),
    [
        (Arch.X86_64, "linux_amd64.json", "linux-x64", "cli_linux_x64.tar.gz"),
        (Arch.AARCH64, "linux_arm64.json", "linux-arm", "cli_linux_arm64.tar.gz"),
    ],
)
def test_fetch_cli_release(arch: Arch, manifest_name: str, url_seg: str, tar_name: str) -> None:
    m_url = f"https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/{manifest_name}"
    tar_url = f"https://storage.googleapis.com/antigravity-public/antigravity-cli/1.5.14-5302690847260672/{url_seg}/{tar_name}"
    client = FakeHttpClient(
        {
            ("GET", m_url): _json_resp(
                m_url,
                {"version": "1.5.14", "url": tar_url, "sha512": "a" * 128},
            )
        }
    )

    rel = fetch_cli_release(client, arch)
    assert rel.package is PackageName.ANTIGRAVITY_CLI
    assert rel.version == "1.5.14"
    assert rel.build_id == "5302690847260672"
    assert rel.pkgver == "1.5.14_5302690847260672"
    assert rel.checksum_kind is ChecksumKind.SHA512
    assert rel.checksum_value == "a" * 128


@pytest.mark.parametrize(
    ("arch", "api_arch", "url_arch"),
    [
        (Arch.X86_64, "linux-x64", "linux-x64"),
        (Arch.AARCH64, "linux-arm64", "linux-arm"),
    ],
)
def test_fetch_ide_release_encodes_spaces_and_ignores_vscode_product_version(
    arch: Arch, api_arch: str, url_arch: str
) -> None:
    endpoint = f"https://antigravity-ide-auto-updater-974169037036.us-central1.run.app/api/update/{api_arch}/stable/latest"
    raw_url = f"https://edgedl.me.gvt1.com/edgedl/release2/j0qc3/antigravity/stable/2.8.0-6733682917113856/{url_arch}/Antigravity IDE.tar.gz"
    client = FakeHttpClient(
        {
            ("GET", endpoint): _json_resp(
                endpoint,
                {
                    "timestamp": 1795119630,
                    "productVersion": "1.107.0",
                    "sha256hash": "b" * 64,
                    "url": raw_url,
                },
            )
        }
    )

    rel = fetch_ide_release(client, arch)
    assert rel.package is PackageName.ANTIGRAVITY_IDE
    assert rel.version == "2.8.0"
    assert rel.pkgver == "2.8.0"
    assert rel.build_id == "6733682917113856"
    assert "Antigravity%20IDE.tar.gz" in rel.download_url
    assert " " not in rel.download_url
    assert rel.checksum_kind is ChecksumKind.SHA256
    assert rel.checksum_value == "b" * 64


def test_fetch_aur_info_degrades_gracefully_when_unreachable() -> None:
    client = FakeHttpClient({})  # Every URL raises ProviderError
    result = fetch_aur_info(client, [PackageName.ANTIGRAVITY, PackageName.ANTIGRAVITY_CLI])
    assert result[PackageName.ANTIGRAVITY].reachable is False
    assert result[PackageName.ANTIGRAVITY].version is None
    assert result[PackageName.ANTIGRAVITY_CLI].reachable is False


def test_prepare_recipe_workspace_three_tier_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    aur_cache = tmp_path / "aur"
    work_dir = tmp_path / "work"

    # Simulate AUR git unreachable
    monkeypatch.setattr(
        "antigravity_arch.providers.aur._sync_aur_git_clone",
        lambda pkg, clone_dir: (False, "simulated AUR 503"),
    )

    # Tier 3: no cache yet -> falls back to bundled cleanroom recipe
    ws_fallback = prepare_recipe_workspace(
        PackageName.ANTIGRAVITY_IDE,
        aur_cache_dir=aur_cache,
        work_dir=work_dir,
    )
    assert ws_fallback.origin is RecipeOrigin.BUNDLED_FALLBACK
    assert (work_dir / "PKGBUILD").is_file()
    assert (work_dir / "antigravity-ide.sh").is_file()

    # Tier 2: cached AUR clone exists -> uses cached clone
    cached_clone = aur_cache / PackageName.ANTIGRAVITY_IDE.value
    cached_clone.mkdir(parents=True)
    (cached_clone / "PKGBUILD").write_text("# cached aur pkgbuild\n", encoding="utf-8")

    ws_cached = prepare_recipe_workspace(
        PackageName.ANTIGRAVITY_IDE,
        aur_cache_dir=aur_cache,
        work_dir=work_dir,
    )
    assert ws_cached.origin is RecipeOrigin.AUR_CACHED
    assert "# cached aur pkgbuild" in (work_dir / "PKGBUILD").read_text(encoding="utf-8")

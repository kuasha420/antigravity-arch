"""Hermetic CLI contract tests (status, check exit codes, dry-run JSON, config)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from antigravity_arch.cli import main
from antigravity_arch.models import (
    Arch,
    AurInfo,
    ChecksumKind,
    InstalledPackage,
    PackageName,
    PackagePlan,
    SyncAction,
    UpstreamRelease,
)


def _sample_plan(action: SyncAction) -> PackagePlan:
    pkg = PackageName.ANTIGRAVITY
    return PackagePlan(
        package=pkg,
        action=action,
        reason="test reason",
        installed=InstalledPackage(pkg, "2.33.0-1"),
        aur=AurInfo(pkg, "2.33.0-1", 1795064408, None, "https://aur.archlinux.org/antigravity.git"),
        upstream=UpstreamRelease(
            package=pkg,
            arch=Arch.X86_64,
            version="2.34.0",
            build_id="5887025308631040",
            pkgver="2.34.0",
            download_url="https://example.invalid/Antigravity.tar.gz",
            checksum_kind=ChecksumKind.CRC32C_B64,
            checksum_value="BqScAQ==",
        ),
        target_pkgver="2.34.0",
        target_pkgrel="1",
    )


def test_cli_check_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = tmp_path / "missing.toml"

    monkeypatch.setattr("antigravity_arch.cli.collect_plans", lambda *a, **kw: [_sample_plan(SyncAction.BUILD_UPSTREAM)])
    rc_updates = main(["--config", str(cfg), "check", "--json"])
    assert rc_updates == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["package"] == "antigravity"
    assert payload[0]["needs_build"] is True

    monkeypatch.setattr("antigravity_arch.cli.collect_plans", lambda *a, **kw: [_sample_plan(SyncAction.UP_TO_DATE)])
    rc_clean = main(["--config", str(cfg), "check"])
    assert rc_clean == 0
    assert "up-to-date" in capsys.readouterr().out


def test_cli_sync_dry_run_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = tmp_path / "missing.toml"
    monkeypatch.setattr("antigravity_arch.sync.collect_plans", lambda *a, **kw: [_sample_plan(SyncAction.BUILD_UPSTREAM)])

    rc = main(["--config", str(cfg), "sync", "--dry-run", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["dry_run"] is True
    assert data["built"] == []
    assert data["plans"][0]["target_version"] == "2.34.0-1"


def test_cli_invalid_package_returns_nonzero(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    rc = main(["--config", str(tmp_path / "none.toml"), "status", "--packages", "bogus-pkg"])
    assert rc == 1
    assert "Unknown package" in capsys.readouterr().err

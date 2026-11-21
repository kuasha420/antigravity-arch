"""Hermetic tests for pacman.conf management, locking, alpm permissions, and pruning."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from antigravity_arch.models import ALL_PACKAGES, LockError, PackageName
from antigravity_arch.pacman import (
    acquire_sync_lock,
    parse_package_filename,
    prune_local_repo,
    prune_source_cache,
)
from antigravity_arch.repo_setup import (
    check_alpm_traversal,
    inject_pacman_conf_include,
    remove_pacman_conf_include,
    render_pacman_dropin,
)

SAMPLE_PACMAN_CONF = """\
[options]
HoldPkg = pacman glibc
Architecture = auto

[core]
Include = /etc/pacman.d/mirrorlist

[extra]
Include = /etc/pacman.d/mirrorlist
"""


def test_parse_package_filename_strict_prefix_isolation() -> None:
    assert (
        parse_package_filename("antigravity-2.34.0-1-x86_64.pkg.tar.zst", PackageName.ANTIGRAVITY)
        == "2.34.0-1"
    )
    # Must NOT match antigravity-cli or antigravity-ide under PackageName.ANTIGRAVITY
    assert (
        parse_package_filename(
            "antigravity-cli-1.5.14_5302690847260672-1-x86_64.pkg.tar.zst",
            PackageName.ANTIGRAVITY,
        )
        is None
    )
    assert (
        parse_package_filename(
            "antigravity-ide-2.8.0-1-x86_64.pkg.tar.zst",
            PackageName.ANTIGRAVITY,
        )
        is None
    )
    assert (
        parse_package_filename(
            "antigravity-cli-1.5.14_5302690847260672-1-x86_64.pkg.tar.zst",
            PackageName.ANTIGRAVITY_CLI,
        )
        == "1.5.14_5302690847260672-1"
    )
    assert (
        parse_package_filename(
            "antigravity-2.34.0-1-x86_64.pkg.tar.zst.sig",
            PackageName.ANTIGRAVITY,
        )
        is None
    )


def test_prune_local_repo_keeps_newest_and_removes_sigs(tmp_path: Path) -> None:
    for ver in ("2.31.0-1", "2.32.0-1", "2.33.0-1", "2.34.0-1"):
        pkg = tmp_path / f"antigravity-{ver}-x86_64.pkg.tar.zst"
        pkg.write_bytes(b"pkg")
        Path(f"{pkg}.sig").write_bytes(b"sig")

    cli_pkg = tmp_path / "antigravity-cli-1.5.14_5302690847260672-1-x86_64.pkg.tar.zst"
    cli_pkg.write_bytes(b"cli")

    removed = prune_local_repo(tmp_path, ALL_PACKAGES, keep_packages=2)
    removed_names = {p.name for p in removed}

    assert "antigravity-2.31.0-1-x86_64.pkg.tar.zst" in removed_names
    assert "antigravity-2.31.0-1-x86_64.pkg.tar.zst.sig" in removed_names
    assert "antigravity-2.32.0-1-x86_64.pkg.tar.zst" in removed_names
    assert (tmp_path / "antigravity-2.33.0-1-x86_64.pkg.tar.zst").exists()
    assert (tmp_path / "antigravity-2.34.0-1-x86_64.pkg.tar.zst").exists()
    assert cli_pkg.exists()


def test_prune_source_cache(tmp_path: Path) -> None:
    f1 = tmp_path / "a.tar.gz"
    f2 = tmp_path / "b.tar.gz"
    f1.write_bytes(b"1")
    f2.write_bytes(b"2")
    os.utime(f1, (1000, 1000))
    os.utime(f2, (2000, 2000))

    removed_one = prune_source_cache(tmp_path, keep_sources=1)
    assert [p.name for p in removed_one] == ["a.tar.gz"]
    assert f2.exists()

    removed_all = prune_source_cache(tmp_path, keep_sources=0)
    assert [p.name for p in removed_all] == ["b.tar.gz"]
    assert not f2.exists()


def test_sync_lock_prevents_concurrent_access(tmp_path: Path) -> None:
    lock_path = tmp_path / "sync.lock"
    with acquire_sync_lock(lock_path), pytest.raises(LockError), acquire_sync_lock(lock_path):
        pass


def test_pacman_conf_include_inject_idempotent_and_uninstall() -> None:
    dropin = Path("/etc/pacman.d/antigravity-local.conf")
    once = inject_pacman_conf_include(SAMPLE_PACMAN_CONF, "antigravity-local", dropin)
    assert once.index("Include = /etc/pacman.d/antigravity-local.conf") < once.index("[core]")

    twice = inject_pacman_conf_include(once, "antigravity-local", dropin)
    assert twice == once

    removed = remove_pacman_conf_include(twice, "antigravity-local")
    assert "antigravity-local" not in removed
    assert "[core]" in removed
    assert "[options]" in removed


def test_render_pacman_dropin_signed_and_unsigned() -> None:
    unsigned = render_pacman_dropin("antigravity-local", Path("/var/cache/pacman/antigravity-local"), signed=False)
    assert "SigLevel = Optional TrustAll" in unsigned
    assert "Server = file:///var/cache/pacman/antigravity-local" in unsigned

    signed = render_pacman_dropin("antigravity-local", Path("/var/cache/pacman/antigravity-local"), signed=True)
    assert "SigLevel = Required DatabaseOptional" in signed


def test_check_alpm_traversal_detects_private_parent(tmp_path: Path) -> None:
    # Check using a chmod-controlled tree under tmp_path (works cleanly on /tmp 1777)
    private_parent = tmp_path / "private_home"
    private_parent.mkdir(mode=0o700)
    os.chmod(private_parent, 0o700)
    repo = private_parent / "repo"
    repo.mkdir(mode=0o755)
    os.chmod(repo, 0o755)

    res = check_alpm_traversal(repo)
    assert res.ok is False
    assert "alpm sandbox" in res.detail

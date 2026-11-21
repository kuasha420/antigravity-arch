"""Hermetic tests for the 4-case vercmp planner and alpm_vercmp parity."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from antigravity_arch.models import (
    ALL_PACKAGES,
    Arch,
    AurInfo,
    ChecksumKind,
    InstalledPackage,
    PackageName,
    SyncAction,
    UpstreamRelease,
)
from antigravity_arch.pacman import alpm_vercmp_python
from antigravity_arch.planner import plan_package, resolve_target_packages


def _make_release(pkg: PackageName, pkgver: str) -> UpstreamRelease:
    return UpstreamRelease(
        package=pkg,
        arch=Arch.X86_64,
        version=pkgver.split("_")[0],
        build_id="999",
        pkgver=pkgver,
        download_url="https://example.invalid/pkg.tar.gz",
        checksum_kind=ChecksumKind.SHA256,
        checksum_value="0" * 64,
    )


def _make_aur(pkg: PackageName, ver: str | None) -> AurInfo:
    return AurInfo(
        package=pkg,
        version=ver,
        last_modified=None,
        out_of_date=None,
        git_url=f"https://aur.archlinux.org/{pkg.value}.git",
    )


@pytest.mark.parametrize(
    ("v1", "v2", "expected"),
    [
        ("2.34.0", "2.33.0", 1),
        ("2.34.0-1", "2.34.0-2", -1),
        ("2.34.0-1", "2.34.0-1", 0),
        ("1.5.14_5302690847260672-1", "1.5.13_5999302598852608-1", 1),
        ("1.5.14_5302690847260672", "1.5.14", 1),
        ("2.10.0", "2.8.0", 1),
        ("1:1.0.0-1", "2.0.0-1", 1),
    ],
)
def test_alpm_vercmp_python_matches_native(v1: str, v2: str, expected: int) -> None:
    assert alpm_vercmp_python(v1, v2) == expected
    if shutil.which("vercmp"):
        out = subprocess.check_output(["vercmp", v1, v2], text=True).strip()
        assert int(out) == expected


def test_planner_four_case_matrix() -> None:
    pkg = PackageName.ANTIGRAVITY

    # Case 0: Not installed, upstream ahead of AUR
    p0 = plan_package(
        installed=InstalledPackage(pkg, None),
        aur=_make_aur(pkg, "2.33.0-1"),
        upstream=_make_release(pkg, "2.34.0"),
        vercmp_fn=alpm_vercmp_python,
    )
    assert p0.action is SyncAction.BUILD_UPSTREAM
    assert p0.target_full_version == "2.34.0-1"

    # Case 1: Upstream strictly newer than installed & AUR
    p1 = plan_package(
        installed=InstalledPackage(pkg, "2.33.0-1"),
        aur=_make_aur(pkg, "2.33.0-1"),
        upstream=_make_release(pkg, "2.34.0"),
        vercmp_fn=alpm_vercmp_python,
    )
    assert p1.action is SyncAction.BUILD_UPSTREAM
    assert p1.target_full_version == "2.34.0-1"

    # Case 2: Same pkgver, AUR pushed -2 packaging fix
    p2 = plan_package(
        installed=InstalledPackage(pkg, "2.34.0-1"),
        aur=_make_aur(pkg, "2.34.0-2"),
        upstream=_make_release(pkg, "2.34.0"),
        vercmp_fn=alpm_vercmp_python,
    )
    assert p2.action is SyncAction.BUILD_AUR
    assert p2.target_full_version == "2.34.0-2"

    # Case 3: Up to date
    p3 = plan_package(
        installed=InstalledPackage(pkg, "2.34.0-1"),
        aur=_make_aur(pkg, "2.34.0-1"),
        upstream=_make_release(pkg, "2.34.0"),
        vercmp_fn=alpm_vercmp_python,
    )
    assert p3.action is SyncAction.UP_TO_DATE
    assert not p3.needs_build

    # Case 3b: Force rebuild when up to date
    p3_force = plan_package(
        installed=InstalledPackage(pkg, "2.34.0-1"),
        aur=_make_aur(pkg, "2.34.0-1"),
        upstream=_make_release(pkg, "2.34.0"),
        force=True,
        vercmp_fn=alpm_vercmp_python,
    )
    assert p3_force.action is SyncAction.BUILD_UPSTREAM

    # Case 4: Upstream rollback -> hold by default, build if --allow-downgrade
    p4_hold = plan_package(
        installed=InstalledPackage(pkg, "2.35.0-1"),
        aur=_make_aur(pkg, "2.34.0-1"),
        upstream=_make_release(pkg, "2.34.0"),
        vercmp_fn=alpm_vercmp_python,
    )
    assert p4_hold.action is SyncAction.HOLD_NEWER_INSTALLED
    assert not p4_hold.needs_build

    p4_allow = plan_package(
        installed=InstalledPackage(pkg, "2.35.0-1"),
        aur=_make_aur(pkg, "2.34.0-1"),
        upstream=_make_release(pkg, "2.34.0"),
        allow_downgrade=True,
        vercmp_fn=alpm_vercmp_python,
    )
    assert p4_allow.action is SyncAction.BUILD_UPSTREAM
    assert p4_allow.target_full_version == "2.34.0-1"


def test_resolve_target_packages_modes() -> None:
    installed = {
        PackageName.ANTIGRAVITY: InstalledPackage(PackageName.ANTIGRAVITY, None),
        PackageName.ANTIGRAVITY_CLI: InstalledPackage(PackageName.ANTIGRAVITY_CLI, "1.5.13_1-1"),
        PackageName.ANTIGRAVITY_IDE: InstalledPackage(PackageName.ANTIGRAVITY_IDE, None),
    }

    assert resolve_target_packages(ALL_PACKAGES, installed) == ALL_PACKAGES
    assert resolve_target_packages("installed", installed) == (PackageName.ANTIGRAVITY_CLI,)
    assert resolve_target_packages(ALL_PACKAGES, installed, installed_only=True) == (
        PackageName.ANTIGRAVITY_CLI,
    )
    assert resolve_target_packages(
        "installed", installed, cli_packages=[PackageName.ANTIGRAVITY_IDE]
    ) == (PackageName.ANTIGRAVITY_IDE,)

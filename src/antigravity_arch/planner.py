"""4-case vercmp sync planner reconciling installed, AUR, and upstream versions."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from .models import (
    ALL_PACKAGES,
    AurInfo,
    InstalledPackage,
    PackageName,
    PackagePlan,
    SyncAction,
    UpstreamRelease,
)
from .pacman import strip_pkgrel
from .pacman import vercmp as default_vercmp


def resolve_target_packages(
    packages_setting: str | tuple[PackageName, ...],
    installed: dict[PackageName, InstalledPackage],
    *,
    cli_packages: Sequence[PackageName] | None = None,
    installed_only: bool = False,
) -> tuple[PackageName, ...]:
    """Resolve the active subset of packages from CLI flags, config.toml, and pacman state."""
    if cli_packages:
        return tuple(dict.fromkeys(cli_packages))

    if installed_only or packages_setting == "installed":
        active = tuple(pkg for pkg in ALL_PACKAGES if installed.get(pkg) and installed[pkg].is_installed)
        return active

    if isinstance(packages_setting, tuple):
        return packages_setting

    return ALL_PACKAGES


def plan_package(
    *,
    installed: InstalledPackage,
    aur: AurInfo,
    upstream: UpstreamRelease,
    force: bool = False,
    allow_downgrade: bool = False,
    vercmp_fn: Callable[[str, str], int] = default_vercmp,
) -> PackagePlan:
    """Compute exact sync action using Arch vercmp across installed, AUR, and upstream."""
    aur_pkgver = strip_pkgrel(aur.version) if aur.version else None
    aur_pkgrel = aur.version.rsplit("-", 1)[1] if (aur.version and "-" in aur.version) else "1"

    # Case 0: Package not yet installed -> install newest of (upstream, AUR)
    if not installed.is_installed or installed.version is None:
        if aur.version is not None and aur_pkgver is not None and vercmp_fn(aur_pkgver, upstream.pkgver) >= 0:
            return PackagePlan(
                package=upstream.package,
                action=SyncAction.BUILD_AUR,
                reason=f"Not installed; AUR {aur.version} matches/leads upstream {upstream.pkgver}",
                installed=installed,
                aur=aur,
                upstream=upstream,
                target_pkgver=aur_pkgver,
                target_pkgrel=aur_pkgrel,
            )
        return PackagePlan(
            package=upstream.package,
            action=SyncAction.BUILD_UPSTREAM,
            reason=f"Not installed; building upstream {upstream.pkgver}-1",
            installed=installed,
            aur=aur,
            upstream=upstream,
            target_pkgver=upstream.pkgver,
            target_pkgrel="1",
        )

    installed_full = installed.version
    installed_pkgver = strip_pkgrel(installed_full)
    upstream_vs_installed = vercmp_fn(upstream.pkgver, installed_pkgver)

    # Case 1: Upstream is strictly ahead of installed pkgver
    if upstream_vs_installed > 0:
        if aur.version is not None and aur_pkgver is not None and vercmp_fn(aur_pkgver, upstream.pkgver) >= 0:
            return PackagePlan(
                package=upstream.package,
                action=SyncAction.BUILD_AUR,
                reason=f"Upgrade {installed_full} -> AUR {aur.version} (matches upstream {upstream.pkgver})",
                installed=installed,
                aur=aur,
                upstream=upstream,
                target_pkgver=aur_pkgver,
                target_pkgrel=aur_pkgrel,
            )
        return PackagePlan(
            package=upstream.package,
            action=SyncAction.BUILD_UPSTREAM,
            reason=f"Upgrade {installed_full} -> upstream {upstream.pkgver}-1",
            installed=installed,
            aur=aur,
            upstream=upstream,
            target_pkgver=upstream.pkgver,
            target_pkgrel="1",
        )

    # Case 2: Same pkgver, but AUR published a newer pkgrel (e.g. -2 packaging fix)
    if upstream_vs_installed == 0:
        if (
            aur.version is not None
            and aur_pkgver is not None
            and vercmp_fn(aur_pkgver, installed_pkgver) == 0
            and vercmp_fn(aur.version, installed_full) > 0
        ):
            return PackagePlan(
                package=upstream.package,
                action=SyncAction.BUILD_AUR,
                reason=f"AUR packaging rebuild available ({installed_full} -> {aur.version})",
                installed=installed,
                aur=aur,
                upstream=upstream,
                target_pkgver=aur_pkgver,
                target_pkgrel=aur_pkgrel,
            )

        if force:
            return PackagePlan(
                package=upstream.package,
                action=SyncAction.BUILD_UPSTREAM,
                reason=f"Forced rebuild of {upstream.pkgver}-1",
                installed=installed,
                aur=aur,
                upstream=upstream,
                target_pkgver=upstream.pkgver,
                target_pkgrel="1",
            )

        return PackagePlan(
            package=upstream.package,
            action=SyncAction.UP_TO_DATE,
            reason=f"Already at latest ({installed_full})",
            installed=installed,
            aur=aur,
            upstream=upstream,
            target_pkgver=installed_pkgver,
            target_pkgrel=installed_full.rsplit("-", 1)[1] if "-" in installed_full else "1",
        )

    # Case 4: Installed version is newer than upstream (e.g., upstream rollback)
    if allow_downgrade:
        return PackagePlan(
            package=upstream.package,
            action=SyncAction.BUILD_UPSTREAM,
            reason=f"Downgrade allowed: {installed_full} -> upstream {upstream.pkgver}-1",
            installed=installed,
            aur=aur,
            upstream=upstream,
            target_pkgver=upstream.pkgver,
            target_pkgrel="1",
        )

    return PackagePlan(
        package=upstream.package,
        action=SyncAction.HOLD_NEWER_INSTALLED,
        reason=(
            f"Installed {installed_full} is newer than upstream {upstream.pkgver} "
            "(holding; pass --allow-downgrade to override)"
        ),
        installed=installed,
        aur=aur,
        upstream=upstream,
        target_pkgver=installed_pkgver,
        target_pkgrel=installed_full.rsplit("-", 1)[1] if "-" in installed_full else "1",
    )


def build_sync_plan(
    *,
    packages: Sequence[PackageName],
    installed: dict[PackageName, InstalledPackage],
    aur_info: dict[PackageName, AurInfo],
    upstream: dict[PackageName, UpstreamRelease],
    force: bool = False,
    allow_downgrade: bool = False,
    vercmp_fn: Callable[[str, str], int] = default_vercmp,
) -> list[PackagePlan]:
    """Plan sync actions for all requested packages."""
    return [
        plan_package(
            installed=installed[pkg],
            aur=aur_info[pkg],
            upstream=upstream[pkg],
            force=force,
            allow_downgrade=allow_downgrade,
            vercmp_fn=vercmp_fn,
        )
        for pkg in packages
    ]

"""AUR RPC v5 client and 3-tier PKGBUILD recipe resolver (live -> cache -> fallback)."""

from __future__ import annotations

import shutil
import subprocess
import urllib.parse
from collections.abc import Sequence
from importlib import resources
from pathlib import Path

from ..models import (
    AurInfo,
    PackageName,
    ProviderError,
    RecipeOrigin,
    RecipeWorkspace,
)
from .base import HttpClient

AUR_RPC_INFO_URL = "https://aur.archlinux.org/rpc/v5/info"
AUR_GIT_BASE = "https://aur.archlinux.org"


def aur_git_url(package: PackageName) -> str:
    return f"{AUR_GIT_BASE}/{package.value}.git"


def fetch_aur_info(
    client: HttpClient,
    packages: Sequence[PackageName],
) -> dict[PackageName, AurInfo]:
    """Query AUR RPC v5 in a single batched call, gracefully degrading on AUR outages."""
    if not packages:
        return {}

    query = urllib.parse.urlencode([("arg[]", pkg.value) for pkg in packages])
    url = f"{AUR_RPC_INFO_URL}?{query}"

    try:
        payload = client.get_json(url)
    except ProviderError as err:
        return {
            pkg: AurInfo(
                package=pkg,
                version=None,
                last_modified=None,
                out_of_date=None,
                git_url=aur_git_url(pkg),
                reachable=False,
                error=str(err),
            )
            for pkg in packages
        }

    results_by_name: dict[str, dict[str, object]] = {}
    raw_results = payload.get("results", []) if isinstance(payload, dict) else []
    if isinstance(raw_results, list):
        for entry in raw_results:
            if isinstance(entry, dict) and isinstance(entry.get("Name"), str):
                results_by_name[entry["Name"]] = entry

    output: dict[PackageName, AurInfo] = {}
    for pkg in packages:
        entry = results_by_name.get(pkg.value)
        if entry is None:
            output[pkg] = AurInfo(
                package=pkg,
                version=None,
                last_modified=None,
                out_of_date=None,
                git_url=aur_git_url(pkg),
                reachable=True,
                error=f"Package {pkg.value!r} not found in AUR",
            )
            continue
        raw_ver = entry.get("Version")
        raw_mod = entry.get("LastModified")
        raw_ood = entry.get("OutOfDate")
        output[pkg] = AurInfo(
            package=pkg,
            version=str(raw_ver) if raw_ver is not None else None,
            last_modified=int(raw_mod) if isinstance(raw_mod, int) else None,
            out_of_date=int(raw_ood) if isinstance(raw_ood, int) else None,
            git_url=aur_git_url(pkg),
            reachable=True,
        )
    return output


def _run_git(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _sync_aur_git_clone(package: PackageName, clone_dir: Path) -> tuple[bool, str | None]:
    """Clone or fast-forward-reset the local AUR mirror; returns (live_ok, error_msg)."""
    git_dir = clone_dir / ".git"
    clone_dir.parent.mkdir(parents=True, exist_ok=True)

    if git_dir.is_dir():
        fetch = _run_git(["fetch", "--depth=1", "origin", "master"], cwd=clone_dir)
        if fetch.returncode != 0:
            return False, fetch.stderr.strip() or f"git fetch failed (exit {fetch.returncode})"
        reset = _run_git(["reset", "--hard", "origin/master"], cwd=clone_dir)
        if reset.returncode != 0:
            return False, reset.stderr.strip() or f"git reset failed (exit {reset.returncode})"
        _run_git(["clean", "-fd"], cwd=clone_dir)
        return True, None

    if clone_dir.exists():
        shutil.rmtree(clone_dir)

    clone = _run_git(["clone", "--depth=1", aur_git_url(package), str(clone_dir)])
    if clone.returncode != 0:
        if clone_dir.exists():
            shutil.rmtree(clone_dir)
        return False, clone.stderr.strip() or f"git clone failed (exit {clone.returncode})"
    return True, None


def _copy_tree_contents(src_dir: Path, dst_dir: Path) -> None:
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        if item.name == ".git":
            continue
        target = dst_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)


def _copy_bundled_fallback(package: PackageName, dst_dir: Path) -> None:
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)

    fallback_root = resources.files("antigravity_arch.assets").joinpath("fallback", package.value)
    with resources.as_file(fallback_root) as fallback_path:
        if not fallback_path.is_dir():
            raise ProviderError(f"Bundled fallback recipe missing for {package.value}")
        _copy_tree_contents(fallback_path, dst_dir)


def prepare_recipe_workspace(
    package: PackageName,
    *,
    aur_cache_dir: Path,
    work_dir: Path,
    aur_version: str | None = None,
    prefer_fallback: bool = False,
) -> RecipeWorkspace:
    """Prepare a clean working copy using the 3-tier recipe cascade.

    Tier 1: Live AUR git fetch/clone (`aur-live`).
    Tier 2: Last known-good cached AUR git clone (`aur-cached`).
    Tier 3: Cleanroom bundled fallback templates (`bundled-fallback`).
    """
    if prefer_fallback:
        _copy_bundled_fallback(package, work_dir)
        return RecipeWorkspace(
            package=package,
            work_dir=work_dir,
            origin=RecipeOrigin.BUNDLED_FALLBACK,
            aur_version=None,
            warning="Using bundled fallback recipe as requested",
        )

    clone_dir = aur_cache_dir / package.value
    live_ok, err_msg = _sync_aur_git_clone(package, clone_dir)
    if live_ok and (clone_dir / "PKGBUILD").is_file():
        _copy_tree_contents(clone_dir, work_dir)
        return RecipeWorkspace(
            package=package,
            work_dir=work_dir,
            origin=RecipeOrigin.AUR_LIVE,
            aur_version=aur_version,
        )

    if (clone_dir / "PKGBUILD").is_file():
        _copy_tree_contents(clone_dir, work_dir)
        return RecipeWorkspace(
            package=package,
            work_dir=work_dir,
            origin=RecipeOrigin.AUR_CACHED,
            aur_version=aur_version,
            warning=f"AUR unreachable ({err_msg}); using cached AUR clone at {clone_dir}",
        )

    _copy_bundled_fallback(package, work_dir)
    return RecipeWorkspace(
        package=package,
        work_dir=work_dir,
        origin=RecipeOrigin.BUNDLED_FALLBACK,
        aur_version=None,
        warning=f"AUR unavailable ({err_msg}) and no local cache; using bundled cleanroom fallback",
    )

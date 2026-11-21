"""End-to-end sync orchestrator connecting providers, verifier, patcher, and pacman."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig
from .integrity import compute_hash_file, verify_file
from .models import (
    ALL_PACKAGES,
    BuildArtifact,
    InstallMode,
    PackageName,
    PackagePlan,
    SyncAction,
)
from .pacman import (
    acquire_sync_lock,
    build_package_with_makepkg,
    parse_package_filename,
    prune_local_repo,
    prune_source_cache,
    publish_to_local_repo,
    query_installed_packages,
    run_pacman_install_direct,
    run_pacman_install_from_repo,
)
from .pkgbuild import inspect_pkgbuild, patch_pkgbuild_for_upstream, patch_pkgbuild_watcher_only
from .planner import build_sync_plan, resolve_target_packages
from .providers import (
    HttpClient,
    fetch_all_upstream,
    fetch_aur_info,
    prepare_recipe_workspace,
)
from .timer import send_staged_notification

LogFn = Callable[[str], None]


@dataclass(frozen=True)
class SyncSummary:
    """Structured result returned by `execute_sync`."""

    plans: tuple[PackagePlan, ...]
    built: tuple[BuildArtifact, ...]
    already_staged: tuple[PackagePlan, ...]
    pruned_packages: tuple[Path, ...]
    pruned_sources: tuple[Path, ...]
    installed: bool


def _is_already_staged(repo_path: Path, package: PackageName, full_version: str) -> Path | None:
    """Return existing built archive in repo_path if package full_version is already staged."""
    if not repo_path.is_dir():
        return None
    for entry in sorted(repo_path.iterdir()):
        if entry.is_file() and parse_package_filename(entry.name, package) == full_version:
            return entry
    return None


def collect_plans(
    config: AppConfig,
    *,
    client: HttpClient | None = None,
    cli_packages: Sequence[PackageName] | None = None,
    installed_only: bool = False,
    force: bool = False,
    allow_downgrade: bool = False,
) -> list[PackagePlan]:
    """Query pacman, AUR RPC v5, and official upstream endpoints to produce sync plans."""
    http = client or HttpClient(timeout=config.http_timeout)
    installed_all = query_installed_packages(ALL_PACKAGES)
    targets = resolve_target_packages(
        config.packages_setting,
        installed_all,
        cli_packages=cli_packages,
        installed_only=installed_only,
    )
    if not targets:
        return []

    aur_info = fetch_aur_info(http, targets)
    upstream = fetch_all_upstream(http, targets, config.arch)
    return build_sync_plan(
        packages=targets,
        installed=installed_all,
        aur_info=aur_info,
        upstream=upstream,
        force=force,
        allow_downgrade=allow_downgrade,
    )


def execute_sync(
    config: AppConfig,
    *,
    client: HttpClient | None = None,
    cli_packages: Sequence[PackageName] | None = None,
    installed_only: bool = False,
    mode_override: InstallMode | None = None,
    install_after: bool = False,
    noconfirm: bool = False,
    dry_run: bool = False,
    force: bool = False,
    allow_downgrade: bool = False,
    prefer_fallback: bool = False,
    gpg_key_override: str | None = None,
    log: LogFn | None = None,
) -> SyncSummary:
    """Execute full query -> verify -> patch -> makepkg -> publish/install -> prune flow."""
    emit: LogFn = log or (lambda msg: print(msg, file=sys.stderr))
    http = client or HttpClient(timeout=config.http_timeout)
    mode = mode_override or config.mode
    gpg_key = gpg_key_override if gpg_key_override is not None else config.gpg_key

    plans = collect_plans(
        config,
        client=http,
        cli_packages=cli_packages,
        installed_only=installed_only,
        force=force,
        allow_downgrade=allow_downgrade,
    )
    if dry_run or not plans:
        return SyncSummary(
            plans=tuple(plans),
            built=(),
            already_staged=(),
            pruned_packages=(),
            pruned_sources=(),
            installed=False,
        )

    built_artifacts: list[BuildArtifact] = []
    already_staged: list[PackagePlan] = []
    direct_pkg_paths: list[Path] = []
    pruned_pkgs: list[Path] = []
    pruned_srcs: list[Path] = []

    with acquire_sync_lock(config.lock_file):
        for plan in plans:
            if not plan.needs_build:
                if plan.action is SyncAction.HOLD_NEWER_INSTALLED:
                    emit(f"[hold] {plan.package.value}: {plan.reason}")
                else:
                    emit(f"[skip] {plan.package.value}: {plan.reason}")
                continue

            if mode is InstallMode.LOCAL_REPO and not force:
                existing = _is_already_staged(
                    config.repo_path, plan.package, plan.target_full_version
                )
                if existing is not None and config.repo_db_path.is_file():
                    emit(
                        f"[staged] {plan.package.value} {plan.target_full_version} "
                        f"already staged in [{config.repo_name}]"
                    )
                    already_staged.append(plan)
                    continue

            emit(f"[plan] {plan.package.value}: {plan.reason}")

            work_dir = config.build_root_dir / "recipes" / plan.package.value
            workspace = prepare_recipe_workspace(
                plan.package,
                aur_cache_dir=config.aur_cache_dir,
                work_dir=work_dir,
                aur_version=plan.aur.version,
                prefer_fallback=prefer_fallback,
            )
            if workspace.warning:
                emit(f"[warn] {plan.package.value}: {workspace.warning}")

            pkgbuild_path = workspace.work_dir / "PKGBUILD"
            pkgbuild_text = pkgbuild_path.read_text(encoding="utf-8")
            watcher_guard_applied = False

            if plan.action is SyncAction.BUILD_UPSTREAM or workspace.origin.value == "bundled-fallback":
                inspection = inspect_pkgbuild(
                    pkgbuild_text,
                    package=plan.package,
                    arch=config.arch,
                    target_pkgver=plan.target_pkgver,
                    target_build_id=plan.upstream.build_id,
                )

                dest_tarball = config.src_cache_dir / inspection.filename
                need_download = True
                if dest_tarball.is_file():
                    try:
                        verify_file(
                            dest_tarball,
                            expected_kind=plan.upstream.checksum_kind,
                            expected_value=plan.upstream.checksum_value,
                            expected_size=plan.upstream.content_length,
                        )
                        need_download = False
                        emit(
                            f"[cache] Reusing verified {dest_tarball.name} "
                            f"({plan.upstream.checksum_kind.value} ok)"
                        )
                    except Exception:
                        dest_tarball.unlink(missing_ok=True)

                if need_download:
                    emit(f"[fetch] Downloading {plan.upstream.download_url} -> {dest_tarball.name}")
                    http.download_file(plan.upstream.download_url, dest_tarball)
                    emit(
                        f"[verify] Verifying {dest_tarball.name} "
                        f"({plan.upstream.checksum_kind.value})..."
                    )
                    verify_file(
                        dest_tarball,
                        expected_kind=plan.upstream.checksum_kind,
                        expected_value=plan.upstream.checksum_value,
                        expected_size=plan.upstream.content_length,
                    )

                makepkg_digest = compute_hash_file(dest_tarball, inspection.sum_kind)
                patch_result = patch_pkgbuild_for_upstream(
                    pkgbuild_text,
                    release=plan.upstream,
                    target_pkgrel=plan.target_pkgrel,
                    target_digest=makepkg_digest,
                    sum_prefix=inspection.sum_prefix,
                )
                pkgbuild_path.write_text(patch_result.content, encoding="utf-8")
                watcher_guard_applied = patch_result.watcher_guard_applied
            else:
                patched_text, watcher_guard_applied = patch_pkgbuild_watcher_only(
                    pkgbuild_text, plan.package
                )
                if patched_text != pkgbuild_text:
                    pkgbuild_path.write_text(patched_text, encoding="utf-8")

            pkg_out_dir = config.build_root_dir / "pkgs"
            build_tmp_dir = config.build_root_dir / "tmp" / plan.package.value
            emit(
                f"[build] Building {plan.package.value} {plan.target_full_version} "
                f"(recipe: {workspace.origin.value})..."
            )
            artifact = build_package_with_makepkg(
                package=plan.package,
                work_dir=workspace.work_dir,
                src_cache_dir=config.src_cache_dir,
                pkg_out_dir=pkg_out_dir,
                build_tmp_dir=build_tmp_dir,
                expected_full_version=plan.target_full_version,
                recipe_origin=workspace.origin,
                watcher_guard_applied=watcher_guard_applied,
                gpg_key=gpg_key,
            )

            if mode is InstallMode.LOCAL_REPO:
                published = publish_to_local_repo(
                    artifact,
                    repo_path=config.repo_path,
                    repo_db_path=config.repo_db_path,
                    gpg_key=gpg_key,
                )
                emit(f"[publish] Added {published.name} to [{config.repo_name}]")
                # Clean staging copy in pkg_out_dir after publish
                if artifact.pkg_path.resolve() != published.resolve():
                    artifact.pkg_path.unlink(missing_ok=True)
                    if artifact.sig_path is not None:
                        artifact.sig_path.unlink(missing_ok=True)
                artifact = BuildArtifact(
                    package=artifact.package,
                    full_version=artifact.full_version,
                    pkg_path=published,
                    sig_path=Path(f"{published}.sig") if Path(f"{published}.sig").is_file() else None,
                    recipe_origin=artifact.recipe_origin,
                    watcher_guard_applied=artifact.watcher_guard_applied,
                )
            else:
                direct_pkg_paths.append(artifact.pkg_path)

            built_artifacts.append(artifact)
            shutil.rmtree(work_dir, ignore_errors=True)

        if built_artifacts:
            if mode is InstallMode.LOCAL_REPO:
                pruned_pkgs = prune_local_repo(
                    config.repo_path,
                    ALL_PACKAGES,
                    keep_packages=config.keep_packages,
                )
            pruned_srcs = prune_source_cache(
                config.src_cache_dir,
                keep_sources=config.keep_sources,
            )

    installed_flag = False
    if install_after:
        if mode is InstallMode.LOCAL_REPO:
            targets_to_install = [a.package for a in built_artifacts] + [
                p.package for p in already_staged
            ]
            if targets_to_install:
                emit(f"[install] Running pacman -Syu for: {', '.join(p.value for p in targets_to_install)}")
                run_pacman_install_from_repo(targets_to_install, noconfirm=noconfirm)
                installed_flag = True
        elif direct_pkg_paths:
            emit(f"[install] Running pacman -U for {len(direct_pkg_paths)} package(s)")
            run_pacman_install_direct(direct_pkg_paths, noconfirm=noconfirm)
            installed_flag = True
    elif mode is InstallMode.LOCAL_REPO and built_artifacts and config.notify:
        send_staged_notification(built_artifacts, config.repo_name)

    return SyncSummary(
        plans=tuple(plans),
        built=tuple(built_artifacts),
        already_staged=tuple(already_staged),
        pruned_packages=tuple(pruned_pkgs),
        pruned_sources=tuple(pruned_srcs),
        installed=installed_flag,
    )

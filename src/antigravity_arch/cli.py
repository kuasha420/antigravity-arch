"""Command-line interface for antigravity-arch / agy-arch."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import __version__
from .config import AppConfig, default_config_path, load_config, write_default_config
from .models import (
    ALL_PACKAGES,
    AntigravityArchError,
    Arch,
    InstallMode,
    PackageName,
    PackagePlan,
)
from .pacman import prune_local_repo, prune_source_cache
from .repo_setup import init_local_repo, is_repo_registered, run_doctor, uninstall_local_repo
from .sync import collect_plans, execute_sync
from .timer import disable_timer, enable_timer, timer_status


def _parse_pkg_args(raw_list: Sequence[str] | None) -> tuple[PackageName, ...] | None:
    if not raw_list:
        return None
    resolved: list[PackageName] = []
    for item in raw_list:
        for part in item.split(","):
            name = part.strip()
            if not name:
                continue
            if name == "all":
                resolved.extend(ALL_PACKAGES)
            else:
                try:
                    resolved.append(PackageName(name))
                except ValueError as err:
                    valid = ", ".join(p.value for p in ALL_PACKAGES)
                    raise AntigravityArchError(
                        f"Unknown package {name!r}. Valid packages: {valid}, all"
                    ) from err
    return tuple(dict.fromkeys(resolved)) if resolved else None


def _plan_to_dict(plan: PackagePlan) -> dict[str, Any]:
    return {
        "package": plan.package.value,
        "action": plan.action.value,
        "reason": plan.reason,
        "installed": plan.installed.version,
        "installed_foreign": plan.installed.is_foreign,
        "aur": plan.aur.version,
        "aur_reachable": plan.aur.reachable,
        "upstream_version": plan.upstream.version,
        "upstream_pkgver": plan.upstream.pkgver,
        "upstream_build_id": plan.upstream.build_id,
        "upstream_url": plan.upstream.download_url,
        "target_version": plan.target_full_version,
        "needs_build": plan.needs_build,
    }


def _print_plan_table(plans: Sequence[PackagePlan]) -> None:
    if not plans:
        print("No target packages selected.")
        return
    headers = ("PACKAGE", "INSTALLED", "AUR", "UPSTREAM", "ACTION")
    rows = [
        (
            p.package.value,
            p.installed.version or "(not installed)",
            p.aur.version or ("(unreachable)" if not p.aur.reachable else "(missing)"),
            p.upstream.pkgver,
            p.action.value,
        )
        for p in plans
    ]
    widths = [max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt.format(*row))


def _apply_common_overrides(config: AppConfig, args: argparse.Namespace) -> AppConfig:
    updates: dict[str, Any] = {}
    if getattr(args, "arch", None):
        updates["arch"] = Arch.from_machine(args.arch)
    if getattr(args, "repo_path", None):
        updates["repo_path"] = Path(args.repo_path)
    if getattr(args, "repo_name", None):
        updates["repo_name"] = args.repo_name
        updates["pacman_dropin"] = Path(f"/etc/pacman.d/{args.repo_name}.conf")
    if getattr(args, "gpg_key", None):
        updates["gpg_key"] = args.gpg_key
    return replace(config, **updates) if updates else config


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="antigravity-arch",
        description="Query, package (pacman), install, and update Google Antigravity on Arch Linux.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config.toml (default: {default_config_path()})",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = sub.add_parser("status", help="Show installed, AUR, and official upstream versions")
    p_status.add_argument("--packages", nargs="+", help="Filter packages (antigravity, antigravity-cli, antigravity-ide)")
    p_status.add_argument("--installed-only", action="store_true", help="Only inspect installed packages")
    p_status.add_argument("--arch", choices=("x86_64", "aarch64"), help="Override target architecture")
    p_status.add_argument("--json", action="store_true", dest="as_json", help="Emit machine-readable JSON")

    # check
    p_check = sub.add_parser("check", help="Exit 0 if up to date, 2 if updates are available")
    p_check.add_argument("--packages", nargs="+")
    p_check.add_argument("--installed-only", action="store_true")
    p_check.add_argument("--arch", choices=("x86_64", "aarch64"))
    p_check.add_argument("--json", action="store_true", dest="as_json")

    # sync
    p_sync = sub.add_parser("sync", help="Query upstream, verify, build, and publish/install updates")
    p_sync.add_argument("--packages", nargs="+", help="Subset of packages to sync")
    p_sync.add_argument("--installed-only", action="store_true", help="Only sync already-installed packages")
    p_sync.add_argument("--arch", choices=("x86_64", "aarch64"), help="Override target architecture")
    p_sync.add_argument("--mode", choices=("local-repo", "direct"), help="Override install mode")
    p_sync.add_argument("--install", action="store_true", help="Also run pacman -Syu / -U after building")
    p_sync.add_argument("--noconfirm", action="store_true", help="Pass --noconfirm to pacman during --install")
    p_sync.add_argument("--dry-run", action="store_true", help="Show planned actions without downloading or building")
    p_sync.add_argument("--force", action="store_true", help="Rebuild even if already at latest version")
    p_sync.add_argument("--allow-downgrade", action="store_true", help="Allow downgrading if installed > upstream")
    p_sync.add_argument("--fallback-recipe", action="store_true", help="Use bundled cleanroom PKGBUILD fallbacks")
    p_sync.add_argument("--gpg-key", help="GPG key ID for signing packages and local repo DB")
    p_sync.add_argument("--json", action="store_true", dest="as_json")

    # repo
    p_repo = sub.add_parser("repo", help="Manage the local file:// pacman repository")
    repo_sub = p_repo.add_subparsers(dest="repo_cmd", required=True)

    p_rinit = repo_sub.add_parser("init", help="Create local repo dir, initialize DB, and register in pacman.conf")
    p_rinit.add_argument("--path", dest="repo_path", help="Local repository directory path")
    p_rinit.add_argument("--name", dest="repo_name", help="Pacman repository section name")
    p_rinit.add_argument("--gpg-key", help="GPG key ID for signed local repo (SigLevel = Required DatabaseOptional)")
    p_rinit.add_argument("--owner", help="Username who owns the local repo directory")

    p_rstat = repo_sub.add_parser("status", help="Inspect local repository registration and staged packages")
    p_rstat.add_argument("--json", action="store_true", dest="as_json")

    p_rprune = repo_sub.add_parser("prune", help="Prune old built packages and cached source tarballs")
    p_rprune.add_argument("--keep-packages", type=int, help="Number of package versions to retain per package")
    p_rprune.add_argument("--keep-sources", type=int, help="Number of cached source tarballs to retain")

    p_runinst = repo_sub.add_parser("uninstall", help="Remove local repo from pacman.conf and delete drop-in")
    p_runinst.add_argument("--purge", action="store_true", help="Also delete the local repo directory and cached packages")

    # timer
    p_timer = sub.add_parser("timer", help="Manage rootless systemd --user background sync timer")
    timer_sub = p_timer.add_subparsers(dest="timer_cmd", required=True)

    p_tenable = timer_sub.add_parser("enable", help="Install and enable the systemd --user timer")
    p_tenable.add_argument("--interval", help="Sync interval (e.g. 6h, 12h, daily)")

    p_tdisable = timer_sub.add_parser("disable", help="Disable the systemd --user timer")
    p_tdisable.add_argument("--purge", action="store_true", help="Also delete unit files from ~/.config/systemd/user")

    timer_sub.add_parser("status", help="Show systemd --user timer status")

    # doctor
    p_doc = sub.add_parser("doctor", help="Check binaries, alpm permissions, repo DB, and configuration")
    p_doc.add_argument("--json", action="store_true", dest="as_json")

    # config
    p_cfg = sub.add_parser("config", help="Manage ~/.config/antigravity-arch/config.toml")
    cfg_sub = p_cfg.add_subparsers(dest="config_cmd", required=True)
    p_cfg_init = cfg_sub.add_parser("init", help="Write default config.toml")
    p_cfg_init.add_argument("--force", action="store_true", help="Overwrite existing config.toml")
    cfg_sub.add_parser("show", help="Print resolved active configuration as JSON")

    return parser


def _cmd_status_or_check(config: AppConfig, args: argparse.Namespace, *, is_check: bool) -> int:
    cli_pkgs = _parse_pkg_args(args.packages)
    plans = collect_plans(
        config,
        cli_packages=cli_pkgs,
        installed_only=args.installed_only,
    )
    if args.as_json:
        print(json.dumps([_plan_to_dict(p) for p in plans], indent=2))
    else:
        _print_plan_table(plans)

    if is_check and any(p.needs_build for p in plans):
        return 2
    return 0


def _cmd_sync(config: AppConfig, args: argparse.Namespace) -> int:
    cli_pkgs = _parse_pkg_args(args.packages)
    mode_override = InstallMode(args.mode) if args.mode else None
    summary = execute_sync(
        config,
        cli_packages=cli_pkgs,
        installed_only=args.installed_only,
        mode_override=mode_override,
        install_after=args.install,
        noconfirm=args.noconfirm,
        dry_run=args.dry_run,
        force=args.force,
        allow_downgrade=args.allow_downgrade,
        prefer_fallback=args.fallback_recipe,
        gpg_key_override=args.gpg_key,
    )

    if args.as_json:
        payload = {
            "dry_run": args.dry_run,
            "plans": [_plan_to_dict(p) for p in summary.plans],
            "built": [
                {
                    "package": a.package.value,
                    "version": a.full_version,
                    "path": str(a.pkg_path),
                    "recipe_origin": a.recipe_origin.value,
                    "watcher_guard_applied": a.watcher_guard_applied,
                }
                for a in summary.built
            ],
            "already_staged": [p.package.value for p in summary.already_staged],
            "pruned_packages": [str(p) for p in summary.pruned_packages],
            "pruned_sources": [str(p) for p in summary.pruned_sources],
            "installed": summary.installed,
        }
        print(json.dumps(payload, indent=2))
    elif args.dry_run:
        _print_plan_table(summary.plans)
    else:
        print(
            f"Sync complete: {len(summary.built)} built, "
            f"{len(summary.already_staged)} already staged, "
            f"{len(summary.pruned_packages)} old pkg(s) pruned, "
            f"{len(summary.pruned_sources)} source(s) pruned."
        )
        if (summary.built or summary.already_staged) and not summary.installed and (mode_override or config.mode) is InstallMode.LOCAL_REPO:
            print(f"Run 'sudo pacman -Syu' to apply updates staged in [{config.repo_name}].")
    return 0


def _cmd_repo(config: AppConfig, args: argparse.Namespace) -> int:
    if args.repo_cmd == "init":
        init_local_repo(config, owner_override=args.owner)
        print(
            f"Initialized local pacman repository [{config.repo_name}] at {config.repo_path}\n"
            f"Drop-in: {config.pacman_dropin} (included in {config.pacman_conf})"
        )
        return 0

    if args.repo_cmd == "status":
        registered = is_repo_registered(config.pacman_conf, config.repo_name, config.pacman_dropin)
        staged = (
            sorted(p.name for p in config.repo_path.iterdir() if p.is_file() and ".pkg.tar" in p.name)
            if config.repo_path.is_dir()
            else []
        )
        info = {
            "repo_name": config.repo_name,
            "repo_path": str(config.repo_path),
            "repo_db_exists": config.repo_db_path.is_file(),
            "registered_in_pacman_conf": registered,
            "dropin_path": str(config.pacman_dropin),
            "staged_files": staged,
        }
        if args.as_json:
            print(json.dumps(info, indent=2))
        else:
            print(f"Repo:       [{config.repo_name}]")
            print(f"Path:       {config.repo_path}")
            print(f"Registered: {'yes' if registered else 'no'} ({config.pacman_dropin})")
            print(f"DB present: {'yes' if info['repo_db_exists'] else 'no'}")
            print(f"Staged ({len(staged)}):")
            for name in staged:
                print(f"  - {name}")
        return 0

    if args.repo_cmd == "prune":
        keep_pkgs = args.keep_packages if args.keep_packages is not None else config.keep_packages
        keep_srcs = args.keep_sources if args.keep_sources is not None else config.keep_sources
        rm_pkgs = prune_local_repo(config.repo_path, ALL_PACKAGES, keep_packages=keep_pkgs)
        rm_srcs = prune_source_cache(config.src_cache_dir, keep_sources=keep_srcs)
        print(f"Pruned {len(rm_pkgs)} old package file(s) and {len(rm_srcs)} cached source(s).")
        return 0

    if args.repo_cmd == "uninstall":
        uninstall_local_repo(config, purge=args.purge)
        print(f"Uninstalled [{config.repo_name}] from {config.pacman_conf}.")
        return 0

    return 1


def _cmd_timer(config: AppConfig, args: argparse.Namespace) -> int:
    if args.timer_cmd == "enable":
        interval = args.interval or config.timer_interval
        svc, tmr = enable_timer(interval)
        print(f"Enabled systemd --user timer ({interval}):\n  {svc}\n  {tmr}")
        return 0
    if args.timer_cmd == "disable":
        disable_timer(purge=args.purge)
        print("Disabled antigravity-arch-sync.timer.")
        return 0
    if args.timer_cmd == "status":
        print(timer_status())
        return 0
    return 1


def _cmd_doctor(config: AppConfig, args: argparse.Namespace) -> int:
    checks = run_doctor(config)
    all_ok = all(c.ok for c in checks)
    if args.as_json:
        print(
            json.dumps(
                {
                    "ok": all_ok,
                    "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
                },
                indent=2,
            )
        )
    else:
        for check in checks:
            mark = "OK  " if check.ok else "FAIL"
            print(f"[{mark}] {check.name:<16} {check.detail}")
    return 0 if all_ok else 1


def _cmd_config(config: AppConfig, args: argparse.Namespace) -> int:
    if args.config_cmd == "init":
        written = write_default_config(args.config, overwrite=args.force)
        print(f"Wrote default configuration to {written}")
        return 0
    if args.config_cmd == "show":
        pkgs = (
            config.packages_setting
            if isinstance(config.packages_setting, str)
            else [p.value for p in config.packages_setting]
        )
        print(
            json.dumps(
                {
                    "mode": config.mode.value,
                    "arch": config.arch.value,
                    "packages": pkgs,
                    "repo_name": config.repo_name,
                    "repo_path": str(config.repo_path),
                    "pacman_conf": str(config.pacman_conf),
                    "pacman_dropin": str(config.pacman_dropin),
                    "keep_packages": config.keep_packages,
                    "keep_sources": config.keep_sources,
                    "gpg_key": config.gpg_key,
                    "notify": config.notify,
                    "timer_interval": config.timer_interval,
                },
                indent=2,
            )
        )
        return 0
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint returning process exit status."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        config = _apply_common_overrides(config, args)

        if args.command == "status":
            return _cmd_status_or_check(config, args, is_check=False)
        if args.command == "check":
            return _cmd_status_or_check(config, args, is_check=True)
        if args.command == "sync":
            return _cmd_sync(config, args)
        if args.command == "repo":
            return _cmd_repo(config, args)
        if args.command == "timer":
            return _cmd_timer(config, args)
        if args.command == "doctor":
            return _cmd_doctor(config, args)
        if args.command == "config":
            return _cmd_config(config, args)
        parser.print_help()
        return 1
    except AntigravityArchError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

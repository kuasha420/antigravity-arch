"""Rootless systemd --user timer management and desktop notifications."""

from __future__ import annotations

import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path
from string import Template

from .config import xdg_config_home
from .models import AntigravityArchError, BuildArtifact

SERVICE_NAME = "antigravity-arch-sync.service"
TIMER_NAME = "antigravity-arch-sync.timer"


def systemd_user_dir() -> Path:
    return xdg_config_home() / "systemd" / "user"


def _resolve_exec_start() -> str:
    found = shutil.which("antigravity-arch")
    if found:
        return f"{found} sync"
    return f"{sys.executable} -m antigravity_arch sync"


def render_service_unit(exec_start: str | None = None) -> str:
    raw = resources.files("antigravity_arch.assets").joinpath(
        "antigravity-arch-sync.service.tmpl"
    ).read_text(encoding="utf-8")
    return Template(raw).substitute(exec_start=exec_start or _resolve_exec_start())


def render_timer_unit(interval: str) -> str:
    raw = resources.files("antigravity_arch.assets").joinpath(
        "antigravity-arch-sync.timer.tmpl"
    ).read_text(encoding="utf-8")
    return Template(raw).substitute(interval=interval)


def _run_systemctl_user(args: list[str]) -> subprocess.CompletedProcess[str]:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        raise AntigravityArchError("systemctl not found on PATH")
    return subprocess.run(
        [systemctl, "--user", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def enable_timer(interval: str, *, unit_dir: Path | None = None, run_systemctl: bool = True) -> tuple[Path, Path]:
    """Install and enable the rootless systemd --user sync timer."""
    target_dir = unit_dir or systemd_user_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    service_path = target_dir / SERVICE_NAME
    timer_path = target_dir / TIMER_NAME
    service_path.write_text(render_service_unit(), encoding="utf-8")
    timer_path.write_text(render_timer_unit(interval), encoding="utf-8")

    if run_systemctl:
        reload_proc = _run_systemctl_user(["daemon-reload"])
        if reload_proc.returncode != 0:
            raise AntigravityArchError(
                f"systemctl --user daemon-reload failed: {reload_proc.stderr.strip()}"
            )
        enable_proc = _run_systemctl_user(["enable", "--now", TIMER_NAME])
        if enable_proc.returncode != 0:
            raise AntigravityArchError(
                f"systemctl --user enable --now {TIMER_NAME} failed: {enable_proc.stderr.strip()}"
            )

    return service_path, timer_path


def disable_timer(*, unit_dir: Path | None = None, purge: bool = False, run_systemctl: bool = True) -> None:
    """Disable (and optionally delete) the rootless systemd --user sync timer."""
    if run_systemctl:
        _run_systemctl_user(["disable", "--now", TIMER_NAME])

    if purge:
        target_dir = unit_dir or systemd_user_dir()
        (target_dir / SERVICE_NAME).unlink(missing_ok=True)
        (target_dir / TIMER_NAME).unlink(missing_ok=True)
        if run_systemctl:
            _run_systemctl_user(["daemon-reload"])


def timer_status() -> str:
    """Return concise status output for the user timer."""
    proc = _run_systemctl_user(["list-timers", "--all", TIMER_NAME])
    if proc.returncode != 0:
        return proc.stderr.strip() or f"{TIMER_NAME} not active"
    return proc.stdout.strip()


def send_staged_notification(artifacts: list[BuildArtifact], repo_name: str) -> bool:
    """Send a best-effort desktop notification via notify-send when updates are staged."""
    if not artifacts:
        return False
    notify_bin = shutil.which("notify-send")
    if notify_bin is None:
        return False
    summary = "Antigravity updates staged"
    details = ", ".join(f"{a.package.value} {a.full_version}" for a in artifacts)
    body = f"Ready in [{repo_name}]: {details}\nRun 'sudo pacman -Syu' to apply."
    proc = subprocess.run(
        [notify_bin, "--app-name=antigravity-arch", summary, body],
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0

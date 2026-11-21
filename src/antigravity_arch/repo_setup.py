"""Idempotent local pacman repository initialization, teardown, and doctor checks."""

from __future__ import annotations

import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from string import Template

from .config import AppConfig
from .models import AntigravityArchError, BuildError

_MANAGED_START = "# >>> antigravity-arch ({repo_name}) >>>"
_MANAGED_END = "# <<< antigravity-arch ({repo_name}) <<<"


@dataclass(frozen=True)
class DoctorCheck:
    """Single diagnostic result returned by `antigravity-arch doctor`."""

    name: str
    ok: bool
    detail: str


def render_pacman_dropin(repo_name: str, repo_path: Path, *, signed: bool) -> str:
    """Render /etc/pacman.d/<repo_name>.conf from packaged template."""
    raw = resources.files("antigravity_arch.assets").joinpath("pacman-repo.conf.tmpl").read_text(
        encoding="utf-8"
    )
    sig_level = "Required DatabaseOptional" if signed else "Optional TrustAll"
    return Template(raw).substitute(
        repo_name=repo_name,
        repo_path=str(repo_path),
        sig_level=sig_level,
    )


def inject_pacman_conf_include(pacman_conf_text: str, repo_name: str, dropin_path: Path) -> str:
    """Idempotently insert/update managed Include block before the first repo section."""
    start_marker = _MANAGED_START.format(repo_name=repo_name)
    end_marker = _MANAGED_END.format(repo_name=repo_name)
    block = f"{start_marker}\nInclude = {dropin_path}\n{end_marker}"

    existing_re = re.compile(
        rf"(?ms)^{re.escape(start_marker)}\n.*?^{re.escape(end_marker)}$"
    )
    if existing_re.search(pacman_conf_text):
        return existing_re.sub(block, pacman_conf_text, count=1)

    # Insert immediately before the first non-[options] repository header
    section_re = re.compile(r"(?m)^\[(?!options\])([^\]]+)\]\s*$")
    match = section_re.search(pacman_conf_text)
    if match:
        prefix = pacman_conf_text[: match.start()].rstrip("\n")
        suffix = pacman_conf_text[match.start() :]
        return f"{prefix}\n\n{block}\n\n{suffix}"

    return pacman_conf_text.rstrip("\n") + f"\n\n{block}\n"


def remove_pacman_conf_include(pacman_conf_text: str, repo_name: str) -> str:
    """Remove the managed Include block cleanly from pacman.conf."""
    start_marker = _MANAGED_START.format(repo_name=repo_name)
    end_marker = _MANAGED_END.format(repo_name=repo_name)
    block_re = re.compile(
        rf"(?ms)\n?^{re.escape(start_marker)}\n.*?^{re.escape(end_marker)}\n?"
    )
    cleaned = block_re.sub("\n", pacman_conf_text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip("\n") + "\n"


def is_repo_registered(pacman_conf: Path, repo_name: str, dropin_path: Path) -> bool:
    """Return True if both the drop-in file and managed Include exist."""
    if not dropin_path.is_file() or not pacman_conf.is_file():
        return False
    text = pacman_conf.read_text(encoding="utf-8", errors="replace")
    return _MANAGED_START.format(repo_name=repo_name) in text


def check_alpm_traversal(repo_path: Path) -> DoctorCheck:
    """Verify Pacman 7's unprivileged `alpm` user can traverse every parent dir and read repo_path."""
    resolved = repo_path.resolve() if repo_path.exists() else repo_path
    ancestors = [*reversed(resolved.parents), resolved]
    problems: list[str] = []

    for idx, part in enumerate(ancestors):
        if not part.exists():
            problems.append(f"{part} does not exist")
            break
        mode = stat.S_IMODE(part.stat().st_mode)
        is_leaf = idx == len(ancestors) - 1
        needed = 0o005 if is_leaf else 0o001
        if (mode & needed) != needed:
            req_str = "o+rx (0755)" if is_leaf else "o+x"
            problems.append(f"{part} has mode {oct(mode)} (needs {req_str} for alpm sandbox)")

    if problems:
        return DoctorCheck("alpm-traversal", False, "; ".join(problems))
    return DoctorCheck("alpm-traversal", True, f"All directories up to {resolved} allow alpm access")


def _resolve_target_owner() -> tuple[str, int, int]:
    """Determine the non-root user who should own the local repo directory."""
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user:
        pw = pwd.getpwnam(sudo_user)
        return pw.pw_name, pw.pw_uid, pw.pw_gid
    pw = pwd.getpwuid(os.getuid())
    return pw.pw_name, pw.pw_uid, pw.pw_gid


def _run_privileged(cmd: list[str]) -> None:
    full_cmd = cmd
    if os.geteuid() != 0:
        sudo_bin = shutil.which("sudo")
        if sudo_bin is None:
            raise BuildError("sudo is required for privileged repository setup")
        full_cmd = [sudo_bin, *cmd]
    proc = subprocess.run(full_cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise BuildError(f"Privileged command failed ({' '.join(cmd)}): {detail}")


def _write_root_file(target: Path, content: str, *, mode: str = "0644") -> None:
    if os.geteuid() == 0:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        os.chmod(target, int(mode, 8))
        return

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tf:
        tf.write(content)
        tmp_path = Path(tf.name)
    try:
        _run_privileged(["install", f"-Dm{mode}", str(tmp_path), str(target)])
    finally:
        tmp_path.unlink(missing_ok=True)


def init_local_repo(config: AppConfig, *, owner_override: str | None = None) -> None:
    """Create local repo directory (owned by non-root user), init empty DB, register in pacman.conf."""
    if not config.pacman_conf.is_file():
        raise AntigravityArchError(f"pacman.conf not found at {config.pacman_conf}")

    if owner_override:
        pw = pwd.getpwnam(owner_override)
        owner_name, uid, gid = pw.pw_name, pw.pw_uid, pw.pw_gid
    else:
        owner_name, uid, gid = _resolve_target_owner()

    # 1. Create repo dir owned by non-root user with 0755 (alpm-safe)
    if os.geteuid() == 0:
        config.repo_path.mkdir(parents=True, exist_ok=True)
        os.chown(config.repo_path, uid, gid)
        os.chmod(config.repo_path, 0o755)
    else:
        _run_privileged(["install", "-d", "-m0755", "-o", owner_name, "-g", str(gid), str(config.repo_path)])

    # 2. Write drop-in and update pacman.conf idempotently
    dropin_content = render_pacman_dropin(
        config.repo_name,
        config.repo_path,
        signed=bool(config.gpg_key),
    )
    _write_root_file(config.pacman_dropin, dropin_content, mode="0644")

    current_conf = config.pacman_conf.read_text(encoding="utf-8")
    updated_conf = inject_pacman_conf_include(current_conf, config.repo_name, config.pacman_dropin)
    if updated_conf != current_conf:
        _write_root_file(config.pacman_conf, updated_conf, mode="0644")

    # 3. Initialize empty pacman DB if not yet present
    if not config.repo_db_path.exists():
        repo_add_bin = shutil.which("repo-add")
        if repo_add_bin is None:
            raise BuildError("repo-add not found on PATH")
        cmd = [repo_add_bin]
        if config.gpg_key:
            cmd.extend(["--sign", "--key", config.gpg_key])
        cmd.append(str(config.repo_db_path))

        if os.geteuid() == 0 and owner_name != "root":
            proc = subprocess.run(
                ["su", "-", owner_name, "-s", "/bin/sh", "-c", subprocess.list2cmdline(cmd)],
                check=False,
                capture_output=True,
                text=True,
            )
        else:
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True)

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise BuildError(f"Failed initializing empty repo DB: {detail}")

        for entry in config.repo_path.iterdir():
            if entry.is_file() and not entry.is_symlink():
                os.chmod(entry, 0o644)


def uninstall_local_repo(config: AppConfig, *, purge: bool = False) -> None:
    """Remove pacman.conf Include, drop-in file, and optionally delete cached repo dir."""
    if config.pacman_conf.is_file():
        current_conf = config.pacman_conf.read_text(encoding="utf-8")
        cleaned = remove_pacman_conf_include(current_conf, config.repo_name)
        if cleaned != current_conf:
            _write_root_file(config.pacman_conf, cleaned, mode="0644")

    if config.pacman_dropin.exists():
        if os.geteuid() == 0:
            config.pacman_dropin.unlink()
        else:
            _run_privileged(["rm", "-f", str(config.pacman_dropin)])

    if purge and config.repo_path.exists():
        if os.geteuid() == 0:
            shutil.rmtree(config.repo_path)
        else:
            _run_privileged(["rm", "-rf", str(config.repo_path)])


def run_doctor(config: AppConfig) -> list[DoctorCheck]:
    """Run environment, tool, permission, and repo registration checks."""
    checks: list[DoctorCheck] = []

    for binary in ("pacman", "makepkg", "vercmp", "repo-add", "git"):
        found = shutil.which(binary)
        checks.append(
            DoctorCheck(
                name=f"bin:{binary}",
                ok=found is not None,
                detail=found or f"{binary} not found on PATH",
            )
        )

    checks.append(
        DoctorCheck(
            name="non-root",
            ok=os.geteuid() != 0,
            detail="Running as non-root user" if os.geteuid() != 0 else "Running as root (makepkg requires non-root)",
        )
    )

    if config.mode.value == "local-repo":
        checks.append(check_alpm_traversal(config.repo_path))
        registered = is_repo_registered(config.pacman_conf, config.repo_name, config.pacman_dropin)
        checks.append(
            DoctorCheck(
                name="pacman-conf",
                ok=registered,
                detail=(
                    f"[{config.repo_name}] registered via {config.pacman_dropin}"
                    if registered
                    else f"[{config.repo_name}] not registered (run 'antigravity-arch repo init')"
                ),
            )
        )
        db_ok = config.repo_db_path.is_file()
        checks.append(
            DoctorCheck(
                name="repo-db",
                ok=db_ok,
                detail=str(config.repo_db_path) if db_ok else f"Missing DB: {config.repo_db_path}",
            )
        )

        if config.repo_path.is_dir():
            writable = os.access(config.repo_path, os.W_OK)
            checks.append(
                DoctorCheck(
                    name="repo-writable",
                    ok=writable,
                    detail=(
                        f"{config.repo_path} is writable by current user"
                        if writable
                        else f"{config.repo_path} is NOT writable by current user"
                    ),
                )
            )

    if config.gpg_key:
        gpg_bin = shutil.which("gpg")
        checks.append(
            DoctorCheck(
                name="gpg-signing",
                ok=gpg_bin is not None,
                detail=f"GPG signing enabled for key {config.gpg_key}" if gpg_bin else "gpg binary missing",
            )
        )

    return checks

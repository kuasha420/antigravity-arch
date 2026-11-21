"""Pacman, vercmp, makepkg, repo-add, advisory locking, and bounded retention."""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from .models import (
    Arch,
    BuildArtifact,
    BuildError,
    InstalledPackage,
    LockError,
    PackageName,
    RecipeOrigin,
)

_PKG_EXT_RE = re.compile(r"\.pkg\.tar(?:\.[a-z0-9]+)?$")


def _split_evr(ver: str) -> tuple[str, str, str | None]:
    epoch = "0"
    rest = ver
    if ":" in rest:
        epoch, rest = rest.split(":", 1)
    if "-" in rest:
        pkgver, pkgrel = rest.rsplit("-", 1)
        return epoch, pkgver, pkgrel
    return epoch, rest, None


def _rpmvercmp(a: str, b: str) -> int:
    """Pure-Python equivalent of libalpm's rpmvercmp for non-Arch CI fallback."""
    if a == b:
        return 0

    i = j = 0
    la, lb = len(a), len(b)

    while i < la and j < lb:
        while i < la and not a[i].isalnum():
            i += 1
        while j < lb and not b[j].isalnum():
            j += 1
        if i >= la or j >= lb:
            break

        if a[i].isdigit():
            i2 = i
            while i2 < la and a[i2].isdigit():
                i2 += 1
            j2 = j
            while j2 < lb and b[j2].isdigit():
                j2 += 1
            if j2 == j:
                return 1
            sa = a[i:i2].lstrip("0")
            sb = b[j:j2].lstrip("0")
            if len(sa) != len(sb):
                return 1 if len(sa) > len(sb) else -1
            if sa != sb:
                return 1 if sa > sb else -1
            i, j = i2, j2
        else:
            i2 = i
            while i2 < la and a[i2].isalpha():
                i2 += 1
            j2 = j
            while j2 < lb and b[j2].isalpha():
                j2 += 1
            if j2 == j:
                return -1
            sa, sb = a[i:i2], b[j:j2]
            if sa != sb:
                return 1 if sa > sb else -1
            i, j = i2, j2

    if i >= la and j >= lb:
        return 0
    if (i >= la and not b[j].isalpha()) or (i < la and a[i].isalpha()):
        return -1
    return 1


def alpm_vercmp_python(ver1: str, ver2: str) -> int:
    """Pure-Python fallback matching Arch Linux vercmp(8) semantics."""
    if ver1 == ver2:
        return 0
    e1, v1, r1 = _split_evr(ver1)
    e2, v2, r2 = _split_evr(ver2)

    ret = _rpmvercmp(e1, e2)
    if ret == 0:
        ret = _rpmvercmp(v1, v2)
        if ret == 0 and r1 is not None and r2 is not None:
            ret = _rpmvercmp(r1, r2)
    return 1 if ret > 0 else (-1 if ret < 0 else 0)


def vercmp(ver1: str, ver2: str) -> int:
    """Compare two Arch package versions using native /usr/bin/vercmp when available."""
    vercmp_bin = shutil.which("vercmp")
    if vercmp_bin is not None:
        proc = subprocess.run(
            [vercmp_bin, ver1, ver2],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            raw = proc.stdout.strip()
            if raw in ("-1", "0", "1"):
                return int(raw)
            try:
                val = int(raw)
                return 1 if val > 0 else (-1 if val < 0 else 0)
            except ValueError as err:
                raise BuildError(f"Unexpected vercmp output: {raw!r}") from err
    return alpm_vercmp_python(ver1, ver2)


def strip_pkgrel(full_version: str) -> str:
    """Return pkgver (and epoch if present) without the trailing -pkgrel."""
    return full_version.rsplit("-", 1)[0] if "-" in full_version else full_version


def query_installed_packages(
    packages: Sequence[PackageName],
) -> dict[PackageName, InstalledPackage]:
    """Query local pacman database for installed versions and foreign (-Qm) status."""
    pacman_bin = shutil.which("pacman")
    if pacman_bin is None:
        return {pkg: InstalledPackage(package=pkg, version=None, is_foreign=False) for pkg in packages}

    foreign_names: set[str] = set()
    qm_proc = subprocess.run(
        [pacman_bin, "-Qm"],
        check=False,
        capture_output=True,
        text=True,
    )
    if qm_proc.returncode == 0:
        for line in qm_proc.stdout.splitlines():
            parts = line.split()
            if parts:
                foreign_names.add(parts[0])

    installed: dict[PackageName, InstalledPackage] = {}
    for pkg in packages:
        proc = subprocess.run(
            [pacman_bin, "-Q", pkg.value],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            parts = proc.stdout.strip().split()
            ver = parts[1] if len(parts) >= 2 else None
            installed[pkg] = InstalledPackage(
                package=pkg,
                version=ver,
                is_foreign=pkg.value in foreign_names,
            )
        else:
            installed[pkg] = InstalledPackage(package=pkg, version=None, is_foreign=False)
    return installed


@contextmanager
def acquire_sync_lock(lock_file: Path) -> Iterator[None]:
    """Acquire non-blocking advisory flock to serialize sync/build/repo-add operations."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as err:
            try:
                holder = os.pread(fd, 64, 0).decode("utf-8", errors="replace").strip()
            except OSError:
                holder = "unknown"
            raise LockError(
                f"Another antigravity-arch process holds {lock_file} (pid: {holder or 'unknown'})"
            ) from err
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode("ascii"), 0)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def parse_package_filename(filename: str, expected_pkg: PackageName) -> str | None:
    """Extract `<pkgver>-<pkgrel>` if filename is a built archive for `expected_pkg`."""
    if filename.endswith(".sig"):
        return None
    stripped = _PKG_EXT_RE.sub("", filename)
    if stripped == filename:
        return None
    # Arch package filenames are strictly: <pkgname>-<pkgver>-<pkgrel>-<arch>
    # where neither pkgver nor pkgrel may contain hyphens.
    parts = stripped.rsplit("-", 3)
    if len(parts) != 4:
        return None
    pkgname, pkgver, pkgrel, arch_part = parts
    if pkgname != expected_pkg.value:
        return None
    if arch_part not in (Arch.X86_64.value, Arch.AARCH64.value, "any"):
        return None
    if not pkgver or not pkgrel:
        return None
    return f"{pkgver}-{pkgrel}"


def build_package_with_makepkg(
    *,
    package: PackageName,
    work_dir: Path,
    src_cache_dir: Path,
    pkg_out_dir: Path,
    build_tmp_dir: Path,
    expected_full_version: str,
    recipe_origin: RecipeOrigin,
    watcher_guard_applied: bool = False,
    gpg_key: str | None = None,
) -> BuildArtifact:
    """Run makepkg as non-root with full checksum validation and isolated staging dirs."""
    if os.geteuid() == 0:
        raise BuildError("Refusing to run makepkg as root. Invoke sync as your regular user.")

    makepkg_bin = shutil.which("makepkg")
    if makepkg_bin is None:
        raise BuildError("makepkg not found on PATH. Install base-devel.")

    src_cache_dir.mkdir(parents=True, exist_ok=True)
    pkg_out_dir.mkdir(parents=True, exist_ok=True)
    if build_tmp_dir.exists():
        shutil.rmtree(build_tmp_dir)
    build_tmp_dir.mkdir(parents=True, exist_ok=True)

    cmd = [makepkg_bin, "-s", "--noconfirm", "--needed", "--force"]
    if gpg_key:
        cmd.extend(["--sign", "--key", gpg_key])

    env = os.environ.copy()
    env["SRCDEST"] = str(src_cache_dir)
    env["PKGDEST"] = str(pkg_out_dir)
    env["BUILDDIR"] = str(build_tmp_dir)

    try:
        proc = subprocess.run(cmd, cwd=work_dir, env=env, check=False)
        if proc.returncode != 0:
            raise BuildError(f"makepkg failed for {package.value} (exit code {proc.returncode})")
    finally:
        if build_tmp_dir.exists():
            shutil.rmtree(build_tmp_dir, ignore_errors=True)

    candidates: list[Path] = []
    for item in sorted(pkg_out_dir.iterdir()):
        ver = parse_package_filename(item.name, package)
        if ver == expected_full_version:
            candidates.append(item)

    if not candidates:
        raise BuildError(
            f"makepkg succeeded for {package.value} {expected_full_version}, "
            f"but no matching .pkg.tar.* was found in {pkg_out_dir}"
        )

    pkg_path = candidates[-1]
    os.chmod(pkg_path, 0o644)

    sig_candidate = Path(f"{pkg_path}.sig")
    sig_path: Path | None = None
    if sig_candidate.is_file():
        os.chmod(sig_candidate, 0o644)
        sig_path = sig_candidate

    return BuildArtifact(
        package=package,
        full_version=expected_full_version,
        pkg_path=pkg_path,
        sig_path=sig_path,
        recipe_origin=recipe_origin,
        watcher_guard_applied=watcher_guard_applied,
    )


def _normalize_repo_permissions(repo_path: Path) -> None:
    """Ensure Pacman 7 alpm user can read all files in the local repository."""
    for entry in repo_path.iterdir():
        if entry.is_symlink():
            continue
        if entry.is_file():
            os.chmod(entry, 0o644)


def publish_to_local_repo(
    artifact: BuildArtifact,
    *,
    repo_path: Path,
    repo_db_path: Path,
    gpg_key: str | None = None,
) -> Path:
    """Move built package into local repo and register it with repo-add."""
    repo_add_bin = shutil.which("repo-add")
    if repo_add_bin is None:
        raise BuildError("repo-add not found on PATH. Install pacman.")

    if not repo_path.is_dir():
        raise BuildError(
            f"Local repository directory does not exist: {repo_path}. "
            "Run 'antigravity-arch repo init' first."
        )

    dest_pkg = repo_path / artifact.pkg_path.name
    if artifact.pkg_path.resolve() != dest_pkg.resolve():
        tmp_dest = repo_path / f".{artifact.pkg_path.name}.tmp"
        shutil.copy2(artifact.pkg_path, tmp_dest)
        os.chmod(tmp_dest, 0o644)
        os.replace(tmp_dest, dest_pkg)

    if artifact.sig_path is not None and artifact.sig_path.is_file():
        dest_sig = repo_path / artifact.sig_path.name
        if artifact.sig_path.resolve() != dest_sig.resolve():
            tmp_sig = repo_path / f".{artifact.sig_path.name}.tmp"
            shutil.copy2(artifact.sig_path, tmp_sig)
            os.chmod(tmp_sig, 0o644)
            os.replace(tmp_sig, dest_sig)

    cmd = [repo_add_bin]
    if gpg_key:
        cmd.extend(["--sign", "--key", gpg_key])
    cmd.extend([str(repo_db_path), str(dest_pkg)])

    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise BuildError(f"repo-add failed for {dest_pkg.name}: {detail}")

    _normalize_repo_permissions(repo_path)
    return dest_pkg


def prune_local_repo(
    repo_path: Path,
    packages: Sequence[PackageName],
    *,
    keep_packages: int,
) -> list[Path]:
    """Keep only the newest `keep_packages` built archives per package in repo_path."""
    if keep_packages < 1:
        raise ValueError("keep_packages must be >= 1")
    if not repo_path.is_dir():
        return []

    removed: list[Path] = []
    entries = list(repo_path.iterdir())

    for pkg in packages:
        matched: list[tuple[str, Path]] = []
        for entry in entries:
            if not entry.is_file():
                continue
            ver = parse_package_filename(entry.name, pkg)
            if ver is not None:
                matched.append((ver, entry))

        # Sort descending by Arch vercmp
        from functools import cmp_to_key

        matched.sort(
            key=cmp_to_key(lambda a, b: vercmp(a[0], b[0])),
            reverse=True,
        )

        for _, old_path in matched[keep_packages:]:
            old_path.unlink(missing_ok=True)
            removed.append(old_path)
            sig = Path(f"{old_path}.sig")
            if sig.exists():
                sig.unlink(missing_ok=True)
                removed.append(sig)

    return removed


def prune_source_cache(src_cache_dir: Path, *, keep_sources: int) -> list[Path]:
    """Prune cached downloaded tarballs beyond `keep_sources` newest files."""
    if keep_sources < 0:
        raise ValueError("keep_sources must be >= 0")
    if not src_cache_dir.is_dir():
        return []

    files = [p for p in src_cache_dir.iterdir() if p.is_file() and not p.name.startswith(".")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    removed: list[Path] = []
    for old in files[keep_sources:]:
        old.unlink(missing_ok=True)
        removed.append(old)
    return removed


def run_pacman_install_from_repo(packages: Sequence[PackageName], *, noconfirm: bool = False) -> None:
    """Run `pacman -Syu --needed` (via sudo if non-root) to install/upgrade staged packages."""
    pacman_bin = shutil.which("pacman")
    if pacman_bin is None:
        raise BuildError("pacman not found on PATH")
    cmd = [pacman_bin, "-Syu", "--needed"]
    if noconfirm:
        cmd.append("--noconfirm")
    cmd.extend(pkg.value for pkg in packages)
    if os.geteuid() != 0:
        sudo_bin = shutil.which("sudo")
        if sudo_bin is None:
            raise BuildError("sudo not found; run pacman -Syu as root manually.")
        cmd = [sudo_bin, *cmd]
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        raise BuildError(f"pacman -Syu failed (exit code {proc.returncode})")


def run_pacman_install_direct(pkg_paths: Sequence[Path], *, noconfirm: bool = False) -> None:
    """Run `pacman -U --needed` (via sudo if non-root) for direct installation mode."""
    if not pkg_paths:
        return
    pacman_bin = shutil.which("pacman")
    if pacman_bin is None:
        raise BuildError("pacman not found on PATH")
    cmd = [pacman_bin, "-U", "--needed"]
    if noconfirm:
        cmd.append("--noconfirm")
    cmd.extend(str(p) for p in pkg_paths)
    if os.geteuid() != 0:
        sudo_bin = shutil.which("sudo")
        if sudo_bin is None:
            raise BuildError("sudo not found; run pacman -U as root manually.")
        cmd = [sudo_bin, *cmd]
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        raise BuildError(f"pacman -U failed (exit code {proc.returncode})")

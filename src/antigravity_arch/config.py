"""XDG-compliant configuration loader and defaults for antigravity-arch."""

from __future__ import annotations

import os
import platform
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .models import ALL_PACKAGES, AntigravityArchError, Arch, InstallMode, PackageName

DEFAULT_REPO_NAME = "antigravity-local"
DEFAULT_REPO_PATH = Path("/var/cache/pacman/antigravity-local")
DEFAULT_KEEP_PACKAGES = 2
DEFAULT_KEEP_SOURCES = 0
DEFAULT_TIMER_INTERVAL = "6h"
DEFAULT_HTTP_TIMEOUT = 20


def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def xdg_cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))


def xdg_state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))


def default_config_path() -> Path:
    return xdg_config_home() / "antigravity-arch" / "config.toml"


@dataclass(frozen=True)
class AppConfig:
    """Runtime configuration resolved from defaults, config.toml, and CLI flags."""

    mode: InstallMode
    arch: Arch
    packages_setting: str | tuple[PackageName, ...]
    repo_name: str
    repo_path: Path
    pacman_conf: Path
    pacman_dropin: Path
    aur_cache_dir: Path
    src_cache_dir: Path
    build_root_dir: Path
    lock_file: Path
    keep_packages: int
    keep_sources: int
    gpg_key: str | None
    notify: bool
    http_timeout: int
    timer_interval: str

    @property
    def repo_db_path(self) -> Path:
        return self.repo_path / f"{self.repo_name}.db.tar.zst"


def _parse_packages_value(raw: object) -> str | tuple[PackageName, ...]:
    if isinstance(raw, str):
        norm = raw.strip().lower()
        if norm == "all":
            return ALL_PACKAGES
        if norm == "installed":
            return "installed"
        parts = [p.strip() for p in norm.split(",") if p.strip()]
        return tuple(PackageName(p) for p in parts)
    if isinstance(raw, list):
        resolved: list[PackageName] = []
        for item in raw:
            if not isinstance(item, str):
                raise AntigravityArchError(f"Invalid package entry in config: {item!r}")
            resolved.append(PackageName(item.strip()))
        if not resolved:
            raise AntigravityArchError("Configured 'packages' list cannot be empty")
        return tuple(dict.fromkeys(resolved))
    raise AntigravityArchError(f"Invalid 'packages' config value: {raw!r}")


def load_config(config_path: Path | None = None) -> AppConfig:
    """Load configuration from defaults overlaid with config.toml if present."""
    path = config_path or default_config_path()
    data: dict[str, object] = {}
    if path.exists():
        try:
            with path.open("rb") as fh:
                loaded = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as err:
            raise AntigravityArchError(f"Failed to read config {path}: {err}") from err
        if not isinstance(loaded, dict):
            raise AntigravityArchError(f"Invalid TOML root in {path}")
        data = loaded

    repo_section = data.get("repo", {})
    sync_section = data.get("sync", {})
    retention_section = data.get("retention", {})
    timer_section = data.get("timer", {})

    if not all(isinstance(s, dict) for s in (repo_section, sync_section, retention_section, timer_section)):
        raise AntigravityArchError(f"Config sections in {path} must be TOML tables")

    assert isinstance(repo_section, dict)
    assert isinstance(sync_section, dict)
    assert isinstance(retention_section, dict)
    assert isinstance(timer_section, dict)

    raw_mode = sync_section.get("mode", InstallMode.LOCAL_REPO.value)
    mode = InstallMode(str(raw_mode))

    raw_arch = sync_section.get("arch", platform.machine())
    arch = Arch.from_machine(str(raw_arch))

    packages_setting = _parse_packages_value(sync_section.get("packages", "all"))

    repo_name = str(repo_section.get("name", DEFAULT_REPO_NAME)).strip()
    if not repo_name or "/" in repo_name or " " in repo_name:
        raise AntigravityArchError(f"Invalid repo.name: {repo_name!r}")

    repo_path = Path(os.path.expanduser(str(repo_section.get("path", DEFAULT_REPO_PATH))))
    pacman_conf = Path(os.path.expanduser(str(repo_section.get("pacman_conf", "/etc/pacman.conf"))))
    pacman_dropin = Path(
        os.path.expanduser(
            str(repo_section.get("pacman_dropin", f"/etc/pacman.d/{repo_name}.conf"))
        )
    )

    state_root = xdg_state_home() / "antigravity-arch"
    cache_root = xdg_cache_home() / "antigravity-arch"
    runtime_root = Path(os.environ.get("XDG_RUNTIME_DIR", str(cache_root)))

    aur_cache_dir = Path(os.path.expanduser(str(sync_section.get("aur_cache_dir", state_root / "aur"))))
    src_cache_dir = Path(os.path.expanduser(str(sync_section.get("src_cache_dir", cache_root / "sources"))))
    build_root_dir = Path(os.path.expanduser(str(sync_section.get("build_root_dir", cache_root / "build"))))
    lock_file = Path(os.path.expanduser(str(sync_section.get("lock_file", runtime_root / "antigravity-arch.lock"))))

    keep_packages = int(retention_section.get("keep_packages", DEFAULT_KEEP_PACKAGES))
    keep_sources = int(retention_section.get("keep_sources", DEFAULT_KEEP_SOURCES))
    if keep_packages < 1:
        raise AntigravityArchError("retention.keep_packages must be >= 1")
    if keep_sources < 0:
        raise AntigravityArchError("retention.keep_sources must be >= 0")

    raw_gpg = repo_section.get("gpg_key")
    gpg_key = str(raw_gpg).strip() if raw_gpg else None
    notify = bool(sync_section.get("notify", True))
    http_timeout = int(sync_section.get("http_timeout", DEFAULT_HTTP_TIMEOUT))
    timer_interval = str(timer_section.get("interval", DEFAULT_TIMER_INTERVAL)).strip()

    return AppConfig(
        mode=mode,
        arch=arch,
        packages_setting=packages_setting,
        repo_name=repo_name,
        repo_path=repo_path,
        pacman_conf=pacman_conf,
        pacman_dropin=pacman_dropin,
        aur_cache_dir=aur_cache_dir,
        src_cache_dir=src_cache_dir,
        build_root_dir=build_root_dir,
        lock_file=lock_file,
        keep_packages=keep_packages,
        keep_sources=keep_sources,
        gpg_key=gpg_key,
        notify=notify,
        http_timeout=http_timeout,
        timer_interval=timer_interval,
    )


def render_default_config_template() -> str:
    """Return the default config.toml template from packaged assets."""
    asset = resources.files("antigravity_arch.assets").joinpath("config.toml.tmpl")
    return asset.read_text(encoding="utf-8")


def write_default_config(config_path: Path | None = None, *, overwrite: bool = False) -> Path:
    """Write the default config.toml file to disk."""
    target = config_path or default_config_path()
    if target.exists() and not overwrite:
        raise AntigravityArchError(f"Config already exists at {target} (pass --force to overwrite)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_default_config_template(), encoding="utf-8")
    return target

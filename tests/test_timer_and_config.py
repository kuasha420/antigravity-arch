"""Hermetic tests for config.toml loading and systemd --user unit rendering."""

from __future__ import annotations

from pathlib import Path

import pytest

from antigravity_arch.config import load_config, write_default_config
from antigravity_arch.models import AntigravityArchError, InstallMode, PackageName
from antigravity_arch.timer import (
    SERVICE_NAME,
    TIMER_NAME,
    disable_timer,
    enable_timer,
    render_service_unit,
    render_timer_unit,
)


def test_write_and_load_default_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    written = write_default_config(cfg_path)
    assert written == cfg_path

    cfg = load_config(cfg_path)
    assert cfg.mode is InstallMode.LOCAL_REPO
    assert cfg.repo_name == "antigravity-local"
    assert cfg.keep_packages == 2
    assert cfg.keep_sources == 0
    assert cfg.timer_interval == "6h"

    with pytest.raises(AntigravityArchError, match="already exists"):
        write_default_config(cfg_path, overwrite=False)


def test_custom_config_packages_installed_and_validation(tmp_path: Path) -> None:
    cfg_path = tmp_path / "custom.toml"
    cfg_path.write_text(
        '[sync]\nmode = "direct"\npackages = "installed"\n[retention]\nkeep_packages = 3\nkeep_sources = 1\n',
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.mode is InstallMode.DIRECT
    assert cfg.packages_setting == "installed"
    assert cfg.keep_packages == 3
    assert cfg.keep_sources == 1

    bad_path = tmp_path / "bad.toml"
    bad_path.write_text("[retention]\nkeep_packages = 0\n", encoding="utf-8")
    with pytest.raises(AntigravityArchError, match="keep_packages"):
        load_config(bad_path)

    list_path = tmp_path / "list.toml"
    list_path.write_text('[sync]\npackages = ["antigravity-cli"]\n', encoding="utf-8")
    cfg_list = load_config(list_path)
    assert cfg_list.packages_setting == (PackageName.ANTIGRAVITY_CLI,)


def test_timer_unit_rendering_and_install_disable(tmp_path: Path) -> None:
    svc_text = render_service_unit("/usr/bin/antigravity-arch sync")
    assert "ExecStart=/usr/bin/antigravity-arch sync" in svc_text

    tmr_text = render_timer_unit("4h")
    assert "OnUnitActiveSec=4h" in tmr_text
    assert "Persistent=true" in tmr_text

    svc_path, tmr_path = enable_timer("6h", unit_dir=tmp_path, run_systemctl=False)
    assert svc_path == tmp_path / SERVICE_NAME
    assert tmr_path == tmp_path / TIMER_NAME
    assert svc_path.is_file()
    assert tmr_path.is_file()

    disable_timer(unit_dir=tmp_path, purge=True, run_systemctl=False)
    assert not svc_path.exists()
    assert not tmr_path.exists()

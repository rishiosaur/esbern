from __future__ import annotations

import stat

from esbern.config import Config


def test_config_round_trip_is_private_and_atomic(tmp_path, monkeypatch) -> None:
    import esbern.config as config_module

    config_dir = tmp_path / "config"
    config_path = config_dir / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    expected = Config(password='quote" and slash\\', restart_xochitl=False)

    config_module.save(expected)

    assert config_module.load() == expected
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert list(config_dir.glob(".config.*")) == []

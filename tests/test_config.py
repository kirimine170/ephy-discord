from __future__ import annotations

from pathlib import Path

import pytest

from ephy_discord.config import load_config
from ephy_discord.permissions import minimal_permission_value

ROOT = Path(__file__).resolve().parents[1]


def test_example_configs_load() -> None:
    example = load_config(ROOT / "configs/ephy-discord.example.toml")
    mock = load_config(ROOT / "configs/local-mock.toml")
    assert example.llm.via_gateway is True
    assert example.llm.base_url == "http://127.0.0.1:8000/v1"
    assert example.cache.dedupe_seconds == 600
    assert example.cache.retention_days == 7
    assert example.rate_limit.user_per_minute == 3
    assert example.rate_limit.channel_per_minute == 10
    assert example.mcp.enabled is False
    assert example.mcp.port == 8791
    assert example.approver_user_ids == frozenset()
    assert mock.karte.mode == "stub"
    assert mock.llm.via_gateway is False


def test_public_and_unknown_settings_are_rejected(tmp_path) -> None:
    public = tmp_path / "public.toml"
    public.write_text(
        """
[discord]
guild_id = 1
allowed_channel_ids = [1]

[llm]
base_url = "https://api.openai.com/v1"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="127.0.0.1"):
        load_config(public)

    unknown = tmp_path / "unknown.toml"
    unknown.write_text(
        (ROOT / "configs/local-mock.toml").read_text(encoding="utf-8") + "\nextra = true\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown"):
        load_config(unknown)


def test_permission_value_is_stable() -> None:
    assert minimal_permission_value() == 2147552256

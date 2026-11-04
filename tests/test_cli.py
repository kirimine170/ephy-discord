from __future__ import annotations

from ephy_discord.__main__ import main
from ephy_discord.mock_llm import MockLlmServer
from ephy_discord.service import KARTE_UNAVAILABLE_NOTE


def test_check_does_not_require_a_token(capsys) -> None:
    code = main(["check", "--config", "configs/local-mock.toml"])
    output = capsys.readouterr().out
    assert code == 0
    assert "DISCORD_BOT_TOKEN=unset" in output or "DISCORD_BOT_TOKEN=set" in output
    assert "super-secret" not in output


def test_answer_command_starts_against_mock_llm(tmp_path, capsys) -> None:
    server = MockLlmServer(reply="モックから返事です．")
    server.start()
    config_path = tmp_path / "mock.toml"
    config_path.write_text(
        f"""
[discord]
guild_id = 1
allowed_channel_ids = [1]

[llm]
base_url = "{server.base_url}"
model = "mock"
timeout_seconds = 5
via_gateway = false

[karte]
mode = "stub"

[cache]
path = "{tmp_path / "cache.sqlite"}"
dedupe_seconds = 600
retention_days = 7

[rate_limit]
user_per_minute = 10
channel_per_minute = 10

[audit]
path = "{tmp_path / "audit.jsonl"}"
""",
        encoding="utf-8",
    )
    try:
        code = main(["answer", "--config", str(config_path), "--text", "自己紹介して"])
    finally:
        server.close()
    captured = capsys.readouterr()
    assert code == 0
    assert "モックから返事です．" in captured.out
    assert KARTE_UNAVAILABLE_NOTE in captured.out
    assert "自己紹介して" not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")

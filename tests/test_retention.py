from __future__ import annotations

import pytest

from ephy_discord.service import Turn
from tests.conftest import ManualClock, make_service

SENTINEL = "SENTINEL_QUERY_should_expire"


@pytest.mark.asyncio
async def test_message_content_is_deleted_after_retention(tmp_path) -> None:
    clock = ManualClock()
    service, _llm, _karte = make_service(tmp_path, clock, retention_days=7)
    await service.answer(Turn(guild_id=1, channel_id=10, user_id=100, text=SENTINEL))
    assert SENTINEL in service.store.message_texts()

    clock.advance(days=7)
    fresh = await service.answer(Turn(guild_id=1, channel_id=10, user_id=100, text="新しい質問"))

    assert fresh.kind == "answer"
    assert SENTINEL not in service.store.message_texts()
    assert "新しい質問" in service.store.message_texts()
    stored_audit = "\n".join(str(row) for row in service.audit.read_lines())
    assert SENTINEL not in stored_audit
    assert any(row["event"] == "answer" for row in service.audit.read_lines())
    await service.aclose()


@pytest.mark.asyncio
async def test_content_inside_the_retention_window_remains(tmp_path) -> None:
    clock = ManualClock()
    service, _llm, _karte = make_service(tmp_path, clock, retention_days=7)
    await service.answer(Turn(guild_id=1, channel_id=10, user_id=100, text=SENTINEL))
    clock.advance(days=6)
    service.store.enforce_retention()
    assert SENTINEL in service.store.message_texts()
    await service.aclose()

from __future__ import annotations

import pytest

from ephy_discord.service import Turn
from tests.conftest import ManualClock, make_service


@pytest.mark.asyncio
async def test_identical_question_reuses_answer_within_ten_minutes(tmp_path) -> None:
    clock = ManualClock()
    service, llm, karte = make_service(tmp_path, clock)
    first = await service.answer(_turn("同じ質問です"))
    second = await service.answer(_turn("同じ質問です", user_id=200))

    assert first.kind == "answer"
    assert second.kind == "cached"
    assert second.text == first.text
    assert llm.calls == [llm.calls[0]]
    assert len(llm.calls) == 1
    assert karte.queries == ["同じ質問です"]

    clock.advance(seconds=600)
    still = await service.answer(_turn("同じ質問です", user_id=300))
    assert still.kind == "cached"
    assert len(llm.calls) == 1

    clock.advance(seconds=1)
    again = await service.answer(_turn("同じ質問です", user_id=400))
    assert again.kind == "answer"
    assert len(llm.calls) == 2
    await service.aclose()


@pytest.mark.asyncio
async def test_different_question_does_not_reuse_the_cache(tmp_path) -> None:
    service, llm, _karte = make_service(tmp_path)
    await service.answer(_turn("ひとつめ"))
    result = await service.answer(_turn("ふたつめ"))
    assert result.kind == "answer"
    assert len(llm.calls) == 2
    await service.aclose()


def _turn(text: str, *, user_id: int = 100, channel_id: int = 10) -> Turn:
    return Turn(guild_id=1, channel_id=channel_id, user_id=user_id, text=text)

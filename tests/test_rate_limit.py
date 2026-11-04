from __future__ import annotations

import pytest

from ephy_discord.service import Turn
from tests.conftest import ManualClock, make_service


@pytest.mark.asyncio
async def test_user_limit_is_three_per_window(tmp_path) -> None:
    clock = ManualClock()
    service, llm, _karte = make_service(tmp_path, clock, user_per_minute=3, channel_per_minute=100)
    kinds = []
    for index in range(4):
        result = await service.answer(_turn(f"質問 {index}"))
        kinds.append(result.kind)

    assert kinds == ["answer", "answer", "answer", "rate_limited"]
    assert len(llm.calls) == 3

    clock.advance(seconds=60)
    allowed = await service.answer(_turn("質問 4"))
    assert allowed.kind == "answer"
    assert len(llm.calls) == 4
    await service.aclose()


@pytest.mark.asyncio
async def test_channel_limit_counts_across_users(tmp_path) -> None:
    service, llm, _karte = make_service(tmp_path, user_per_minute=100, channel_per_minute=2)
    first = await service.answer(_turn("a", user_id=1))
    second = await service.answer(_turn("b", user_id=2))
    third = await service.answer(_turn("c", user_id=3))

    assert [first.kind, second.kind, third.kind] == ["answer", "answer", "rate_limited"]
    assert len(llm.calls) == 2
    reasons = [
        row.get("reason") for row in service.audit.read_lines() if row["event"] == "rate_limited"
    ]
    assert reasons == ["channel"]
    await service.aclose()


@pytest.mark.asyncio
async def test_user_limit_follows_the_user_across_channels(tmp_path) -> None:
    service, _llm, _karte = make_service(
        tmp_path,
        channels=(10, 11),
        user_per_minute=2,
        channel_per_minute=100,
    )
    await service.answer(_turn("a", channel_id=10))
    await service.answer(_turn("b", channel_id=11))
    blocked = await service.answer(_turn("c", channel_id=11))
    assert blocked.kind == "rate_limited"
    assert service.audit.read_lines()[-1]["reason"] == "user"
    await service.aclose()


def _turn(text: str, *, user_id: int = 100, channel_id: int = 10) -> Turn:
    return Turn(guild_id=1, channel_id=channel_id, user_id=user_id, text=text)

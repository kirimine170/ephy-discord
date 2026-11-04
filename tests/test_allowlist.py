from __future__ import annotations

import pytest

from ephy_discord.allowlist import Allowlist
from ephy_discord.service import Turn
from tests.conftest import make_service


def test_empty_allowlist_fails_closed() -> None:
    allowlist = Allowlist(guild_id=1, channel_ids=frozenset())
    assert allowlist.permits(1, 10) is False
    assert allowlist.permits(None, 10) is False


def test_only_configured_guild_and_channel_pass() -> None:
    allowlist = Allowlist(guild_id=1, channel_ids=frozenset({10, 11}))
    assert allowlist.permits(1, 10) is True
    assert allowlist.permits(1, 12) is False
    assert allowlist.permits(2, 10) is False


@pytest.mark.asyncio
async def test_service_does_not_answer_or_store_outside_allowlist(tmp_path) -> None:
    service, llm, karte = make_service(tmp_path)
    wrong_channel = await service.answer(
        Turn(guild_id=1, channel_id=99, user_id=5, text="秘密の質問")
    )
    wrong_guild = await service.answer(
        Turn(guild_id=2, channel_id=10, user_id=5, text="秘密の質問")
    )

    assert wrong_channel.kind == "denied"
    assert wrong_guild.kind == "denied"
    assert llm.calls == []
    assert karte.queries == []
    assert service.store.message_texts() == []
    await service.aclose()

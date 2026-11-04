from __future__ import annotations

from types import SimpleNamespace

import pytest

from ephy_discord.approval import NO_APPROVERS, STUB_ACK
from ephy_discord.bot import press_approval, slash_propose
from tests.conftest import ManualClock, make_service


@pytest.mark.asyncio
async def test_only_approvers_can_release_a_reply(tmp_path) -> None:
    clock = ManualClock()
    service, _llm, _karte = make_service(tmp_path, clock, approvers=(7,))
    bot = SimpleNamespace(config=service.config, service=service, approvals=None)
    from ephy_discord.approval import ApprovalQueue

    bot.approvals = ApprovalQueue(service.config, service.store)
    action = bot.approvals.propose(
        guild_id=1,
        channel_id=10,
        requester_user_id=3,
        kind="reply",
        payload={"text": "公開してよい返信"},
    )
    stranger = _interaction(user_id=3, channel_id=10)
    await press_approval(bot, stranger, action["id"], "approved")
    assert stranger.response.messages == ["承認者だけが操作できます．"]
    assert bot.approvals.store.get_action(action["id"])["status"] == "pending"

    approver = _interaction(user_id=7, channel_id=10)
    await press_approval(bot, approver, action["id"], "approved")
    assert approver.channel.sent == ["公開してよい返信"]
    assert bot.approvals.store.get_action(action["id"])["status"] == "approved"
    audit = service.audit.path.read_text(encoding="utf-8")
    assert "action_decided" in audit
    assert "公開してよい返信" not in audit
    await service.aclose()


@pytest.mark.asyncio
async def test_stub_approval_does_not_send_and_expiry_blocks_decision(tmp_path) -> None:
    clock = ManualClock()
    service, _llm, _karte = make_service(tmp_path, clock, approvers=(7,))
    from ephy_discord.approval import ApprovalQueue

    queue = ApprovalQueue(service.config, service.store)
    stub = queue.propose(
        guild_id=1,
        channel_id=10,
        requester_user_id=3,
        kind="stub",
        payload={"name": "grok_search", "text": "調べて"},
    )
    bot = SimpleNamespace(config=service.config, service=service, approvals=queue)
    approver = _interaction(user_id=7, channel_id=10)
    await press_approval(bot, approver, stub["id"], "approved")
    assert approver.channel.sent == []
    assert approver.response.messages == [STUB_ACK]

    reply = queue.propose(
        guild_id=1,
        channel_id=10,
        requester_user_id=3,
        kind="reply",
        payload={"text": "遅い返信"},
    )
    clock.advance(seconds=900)
    late = _interaction(user_id=7, channel_id=10)
    await press_approval(bot, late, reply["id"], "approved")
    assert late.channel.sent == []
    assert "期限切れ" in late.response.messages[0]

    failing = queue.propose(
        guild_id=1,
        channel_id=10,
        requester_user_id=3,
        kind="reply",
        payload={"text": "届いたか不明"},
    )
    broken = _interaction(user_id=7, channel_id=10)
    broken.channel = _BrokenChannel()
    await press_approval(bot, broken, failing["id"], "approved")
    assert broken.channel.sent == []
    assert "再送しません" in broken.response.messages[0]
    again = _interaction(user_id=7, channel_id=10)
    await press_approval(bot, again, failing["id"], "approved")
    assert again.channel.sent == []
    await service.aclose()


@pytest.mark.asyncio
async def test_propose_without_approvers_does_not_queue(tmp_path) -> None:
    service, _llm, _karte = make_service(tmp_path)
    from ephy_discord.approval import ApprovalQueue

    bot = SimpleNamespace(
        config=service.config,
        service=service,
        approvals=ApprovalQueue(service.config, service.store),
    )
    interaction = _interaction(user_id=3, channel_id=10)
    await slash_propose(interaction, "本文", "reply", bot=bot)
    assert interaction.response.messages == [NO_APPROVERS]
    assert service.store.pending_actions() == []
    await service.aclose()


class _Channel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.guild = SimpleNamespace(id=1)
        self.sent: list[str] = []

    async def send(self, content, **kwargs) -> None:
        del kwargs
        self.sent.append(content)


class _BrokenChannel(_Channel):
    def __init__(self) -> None:
        super().__init__(10)

    async def send(self, content, **kwargs) -> None:
        del content, kwargs
        raise RuntimeError("unknown")


class _Response:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.done = False

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, content, **kwargs) -> None:
        del kwargs
        self.messages.append(content)
        self.done = True


class _Interaction:
    def __init__(self, user_id: int, channel_id: int) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = 1
        self.channel_id = channel_id
        self.channel = _Channel(channel_id)
        self.response = _Response()
        self.followup = self.response


def _interaction(user_id: int, channel_id: int) -> _Interaction:
    return _Interaction(user_id, channel_id)

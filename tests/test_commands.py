from __future__ import annotations

from types import SimpleNamespace

import pytest

from ephy_discord.bot import (
    build_command_group,
    command_names,
    handle_incoming_message,
    slash_ask,
    slash_forget,
    slash_purge,
    slash_stop,
)
from ephy_discord.handlers import (
    CHANNEL_DENIED,
    FORGET_ACK,
    LLM_ERROR,
    NOT_ADMIN,
    PURGE_ACK,
    RATE_LIMITED,
    STOP_ACK,
    USAGE,
    handle_ask,
    handle_forget,
    handle_purge,
    handle_stop,
)
from ephy_discord.karte import ContextPack, SourceRef
from ephy_discord.llm import LlmError
from tests.conftest import FakeCtx, make_config, make_service


@pytest.mark.asyncio
async def test_mention_answers_in_allowed_channel_with_karte_context(tmp_path) -> None:
    service, llm, karte = make_service(tmp_path)
    karte.pack = ContextPack(
        status="ok",
        sources=(
            SourceRef(
                doc_id="doc:synthetic-context-001",
                title="Synthetic context boundary",
                relative_path="content/projects/ephy/decision/2026-09/synthetic-context.md",
                excerpt="Karte owns durable Personal Context.",
                sha256="a" * 64,
            ),
        ),
    )
    ctx = FakeCtx()
    result = await handle_ask(ctx, service, "境界は？")

    assert result.kind == "answer"
    assert ctx.replies[0]["ephemeral"] is False
    assert "Synthetic context boundary" in llm.calls[0][0]["content"]
    assert "Karte owns durable Personal Context." in llm.calls[0][0]["content"]
    await service.aclose()


@pytest.mark.asyncio
async def test_handlers_cover_forget_purge_stop_and_failures(tmp_path) -> None:
    service, llm, karte = make_service(tmp_path, admins=(1,))
    owner = FakeCtx(user_id=100)
    await handle_ask(owner, service, "SENTINEL_QUERY_owner")
    other = FakeCtx(user_id=200)
    await handle_ask(other, service, "SENTINEL_QUERY_other")

    await handle_forget(owner, service)
    texts = service.store.message_texts()
    assert "SENTINEL_QUERY_owner" not in texts
    assert "SENTINEL_QUERY_other" in texts
    assert owner.replies[-1]["content"] == FORGET_ACK

    stranger = FakeCtx(user_id=300, is_admin=False)
    await handle_purge(stranger, service)
    assert "SENTINEL_QUERY_other" in service.store.message_texts()
    assert stranger.replies[-1]["content"] == NOT_ADMIN

    admin = FakeCtx(user_id=1, is_admin=True)
    await handle_purge(admin, service)
    assert service.store.message_texts() == []
    assert admin.replies[-1]["content"] == PURGE_ACK

    await handle_stop(stranger, service)
    assert service.stopped is False
    assert stranger.shutdowns == 0

    await handle_stop(admin, service)
    assert service.stopped is True
    assert admin.shutdowns == 1
    assert admin.replies[-1]["content"] == STOP_ACK
    stopped = await handle_ask(other, service, "まだ聞いていい？")
    assert stopped.kind == "stopped"
    assert len(llm.calls) == 2

    service.stopped = False
    llm.error = LlmError("llm_unavailable")
    karte.pack = ContextPack(status="unavailable", sources=())
    failed = FakeCtx(user_id=400)
    await handle_ask(failed, service, "失敗する質問")
    assert failed.replies[-1]["content"] == LLM_ERROR

    limited = FakeCtx(user_id=400)
    await handle_ask(limited, service, "失敗する質問の次")
    await handle_ask(limited, service, "さらに次")
    blocked = FakeCtx(user_id=400)
    await handle_ask(blocked, service, "制限にかかる")
    assert blocked.replies[-1]["content"] == RATE_LIMITED
    await service.aclose()


@pytest.mark.asyncio
async def test_discord_mentions_and_slash_commands_stay_inside_the_allowlist(tmp_path) -> None:
    service, llm, _karte = make_service(tmp_path, admins=(7,))
    config = service.config
    bot_user = SimpleNamespace(id=42)

    ignored = _message("こんにちは", mentions=[], channel_id=10)
    await handle_incoming_message(ignored, config=config, service=service, bot_user=bot_user)
    outsider = _message("<@42> 質問", mentions=[SimpleNamespace(id=42)], channel_id=99)
    await handle_incoming_message(outsider, config=config, service=service, bot_user=bot_user)
    from_bot = _message(
        "<@42> 質問", mentions=[SimpleNamespace(id=42)], channel_id=10, author_bot=True
    )
    await handle_incoming_message(from_bot, config=config, service=service, bot_user=bot_user)
    assert llm.calls == []
    assert ignored.replies == []
    assert outsider.replies == []

    allowed = _message(
        "<@42> 今日の境界は？",
        mentions=[SimpleNamespace(id=42)],
        channel_id=10,
        author_id=5,
    )
    await handle_incoming_message(allowed, config=config, service=service, bot_user=bot_user)
    assert allowed.replies
    assert allowed.replies[0][1]["mention_author"] is False
    assert allowed.replies[0][1]["allowed_mentions"].everyone is False
    assert len(llm.calls) == 1

    denied = _interaction(guild_id=1, channel_id=99, user_id=5)
    await slash_ask(denied, "外の質問", config=config, service=service)
    assert denied.response.messages == [CHANNEL_DENIED]
    assert len(llm.calls) == 1

    empty = _interaction(guild_id=1, channel_id=10, user_id=5)
    await slash_ask(empty, "   ", config=config, service=service)
    assert empty.followup.messages[0]["content"] == USAGE

    forget_user = _interaction(guild_id=1, channel_id=99, user_id=5)
    await slash_forget(forget_user, config=config, service=service)
    assert forget_user.response.messages == [FORGET_ACK]
    assert "今日の境界は？" not in service.store.message_texts()

    purge = _interaction(guild_id=1, channel_id=10, user_id=7, admin=True)
    await slash_purge(purge, config=config, service=service)
    assert purge.response.messages == [PURGE_ACK]

    stop = _interaction(guild_id=1, channel_id=99, user_id=7, admin=True)
    await slash_stop(stop, config=config, service=service)
    assert stop.closed is True
    assert stop.response.messages == [STOP_ACK]

    names = command_names(build_command_group(SimpleNamespace(config=config, service=service)))
    assert names == ["ask", "forget", "propose", "purge", "stop"]
    await service.aclose()


@pytest.mark.asyncio
async def test_long_mention_reply_is_split_and_does_not_leave_the_message(tmp_path) -> None:
    service, llm, _karte = make_service(tmp_path)
    llm.text = "あ" * 4000
    message = _message("<@42> 長く", mentions=[SimpleNamespace(id=42)], channel_id=10)
    await handle_incoming_message(
        message,
        config=service.config,
        service=service,
        bot_user=SimpleNamespace(id=42),
    )
    assert len(message.replies) >= 2
    assert "".join(part[0] for part in message.replies) == "あ" * 4000
    await service.aclose()


def test_example_config_matches_make_config_shape(tmp_path) -> None:
    config = make_config(tmp_path)
    assert config.guild_id == 1
    assert 10 in config.allowed_channel_ids


class _Message:
    def __init__(self, content, mentions, channel_id, author_bot=False, author_id=8) -> None:
        self.content = content
        self.mentions = mentions
        self.guild = SimpleNamespace(id=1)
        self.channel = SimpleNamespace(id=channel_id)
        self.author = SimpleNamespace(
            id=author_id,
            bot=author_bot,
            guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
        )
        self.id = 50
        self.replies: list[tuple] = []

    async def reply(self, content, **kwargs):
        self.replies.append((content, kwargs))


def _message(content, mentions, channel_id, author_bot=False, author_id=8) -> _Message:
    return _Message(content, mentions, channel_id, author_bot, author_id)


class _Response:
    def __init__(self) -> None:
        self.done = False
        self.messages: list[str] = []

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, content, *, ephemeral=False, allowed_mentions=None) -> None:
        del ephemeral, allowed_mentions
        self.messages.append(content)
        self.done = True

    async def defer(self, *, thinking=False, ephemeral=False) -> None:
        del thinking, ephemeral
        self.done = True


class _Followup:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, content, *, ephemeral=False, allowed_mentions=None) -> None:
        del allowed_mentions
        self.messages.append({"content": content, "ephemeral": ephemeral})


class _Interaction:
    def __init__(self, guild_id, channel_id, user_id, admin) -> None:
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.user = SimpleNamespace(
            id=user_id,
            guild_permissions=SimpleNamespace(administrator=admin, manage_guild=False),
        )
        self.response = _Response()
        self.followup = _Followup()
        self.closed = False
        self.client = SimpleNamespace(close=self._close)

    async def _close(self) -> None:
        self.closed = True


def _interaction(guild_id, channel_id, user_id, admin=False) -> _Interaction:
    return _Interaction(guild_id, channel_id, user_id, admin)

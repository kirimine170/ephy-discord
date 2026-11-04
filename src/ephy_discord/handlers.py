"""Command behavior shared by mentions and slash commands."""

from __future__ import annotations

from typing import Protocol

from ephy_discord.config import AppConfig
from ephy_discord.service import AdapterService, AnswerResult, Turn

CHANNEL_DENIED = "このチャンネルでは応答しません．"
GUILD_DENIED = "このサーバーでは応答しません．"
USAGE = "質問を書いてください．メンションするか，`/ephy ask` を使ってください．"
TOO_LONG = "質問が長すぎます．短くしてもう一度送ってください．"
RATE_LIMITED = "少し待ってからもう一度話しかけてください．"
LLM_ERROR = "いまローカルモデルに接続できません．"
STOPPED = "いま停止中です．"
STOP_ACK = "停止します．プロセスを再起動するまで応答しません．"
FORGET_ACK = "あなたの保存データを削除しました．"
PURGE_ACK = "保存しているメッセージ内容と監査ログを削除しました．"
NOT_ADMIN = "管理者だけが実行できます．"


class Responder(Protocol):
    guild_id: int
    channel_id: int
    user_id: int
    is_admin: bool

    async def reply(self, content: str, *, ephemeral: bool = False) -> None: ...

    async def shutdown(self) -> None: ...


def is_actor_admin(user_id: int, permissions: object, config: AppConfig) -> bool:
    if user_id in config.admin_user_ids:
        return True
    if permissions is None:
        return False
    return bool(
        getattr(permissions, "administrator", False) or getattr(permissions, "manage_guild", False)
    )


async def handle_ask(ctx: Responder, service: AdapterService, question: str) -> AnswerResult:
    result = await service.answer(
        Turn(
            guild_id=ctx.guild_id,
            channel_id=ctx.channel_id,
            user_id=ctx.user_id,
            text=question,
            message_id=getattr(ctx, "message_id", None),
        )
    )
    rendered = _render_answer(result)
    if rendered is not None:
        text, ephemeral = rendered
        await ctx.reply(text, ephemeral=ephemeral)
    return result


async def handle_forget(ctx: Responder, service: AdapterService) -> None:
    if ctx.guild_id != service.config.guild_id:
        await ctx.reply(GUILD_DENIED, ephemeral=True)
        return
    service.forget(ctx.user_id)
    await ctx.reply(FORGET_ACK, ephemeral=True)


async def handle_purge(ctx: Responder, service: AdapterService) -> None:
    if ctx.guild_id != service.config.guild_id:
        await ctx.reply(GUILD_DENIED, ephemeral=True)
        return
    if not ctx.is_admin:
        await ctx.reply(NOT_ADMIN, ephemeral=True)
        return
    service.purge(ctx.user_id)
    await ctx.reply(PURGE_ACK, ephemeral=True)


async def handle_stop(ctx: Responder, service: AdapterService) -> None:
    if ctx.guild_id != service.config.guild_id:
        await ctx.reply(GUILD_DENIED, ephemeral=True)
        return
    if not ctx.is_admin:
        await ctx.reply(NOT_ADMIN, ephemeral=True)
        return
    service.stop()
    service.audit.write(
        {
            "ts": _ts(service),
            "event": "stop",
            "user_id": str(ctx.user_id),
            "channel_id": str(ctx.channel_id),
            "guild_id": str(ctx.guild_id),
        }
    )
    await ctx.reply(STOP_ACK, ephemeral=True)
    await ctx.shutdown()


def _render_answer(result: AnswerResult) -> tuple[str, bool] | None:
    if result.kind == "denied":
        return None
    if result.kind in {"answer", "cached"}:
        return result.text, False
    if result.kind == "empty":
        return USAGE, True
    if result.kind == "too_long":
        return TOO_LONG, True
    if result.kind == "rate_limited":
        return RATE_LIMITED, True
    if result.kind == "error":
        return LLM_ERROR, True
    if result.kind == "stopped":
        return STOPPED, True
    return LLM_ERROR, True


def _ts(service: AdapterService) -> str:
    from ephy_discord.store import format_time

    return format_time(service.store.clock.now())

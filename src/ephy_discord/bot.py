"""discord.py adapter. Outbound traffic is a reply in an allowed channel."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ephy_discord.approval import NO_APPROVERS, REAL_KIND, STUB_KIND, ApprovalQueue
from ephy_discord.config import AppConfig
from ephy_discord.handlers import (
    CHANNEL_DENIED,
    handle_ask,
    handle_forget,
    handle_purge,
    handle_stop,
    is_actor_admin,
)
from ephy_discord.mcp import McpApp, load_mcp_secrets, mcp_can_listen, serve_mcp
from ephy_discord.permissions import (
    INTENT_GUILD_MESSAGES,
    INTENT_GUILDS,
    INTENT_MESSAGE_CONTENT,
)
from ephy_discord.service import AdapterService
from ephy_discord.store import format_time
from ephy_discord.text import extract_question, split_discord_text

log = logging.getLogger(__name__)


def build_intents() -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.message_content = True
    return intents


def minimal_permissions() -> discord.Permissions:
    permissions = discord.Permissions.none()
    permissions.view_channel = True
    permissions.send_messages = True
    permissions.read_message_history = True
    permissions.use_application_commands = True
    return permissions


class MentionResponder:
    def __init__(self, message: discord.Message, config: AppConfig) -> None:
        self._message = message
        self._config = config
        self.guild_id = message.guild.id if message.guild is not None else 0
        self.channel_id = message.channel.id
        self.user_id = message.author.id
        self.message_id = str(message.id)
        permissions = getattr(message.author, "guild_permissions", None)
        self.is_admin = is_actor_admin(self.user_id, permissions, config)

    async def reply(self, content: str, *, ephemeral: bool = False) -> None:
        del ephemeral
        if not self._config.allowlist.permits(self.guild_id, self.channel_id):
            raise RuntimeError("refusing to post outside the allowlist")
        mentions = discord.AllowedMentions.none()
        for chunk in split_discord_text(content):
            await self._message.reply(
                chunk,
                allowed_mentions=mentions,
                mention_author=False,
            )

    async def shutdown(self) -> None:
        return None


class InteractionResponder:
    def __init__(self, interaction: discord.Interaction, config: AppConfig) -> None:
        self._interaction = interaction
        self._config = config
        self.guild_id = interaction.guild_id or 0
        self.channel_id = interaction.channel_id or 0
        self.user_id = interaction.user.id
        self.message_id = None
        self.is_admin = is_actor_admin(self.user_id, _actor_permissions(interaction), config)

    async def reply(self, content: str, *, ephemeral: bool = False) -> None:
        if not ephemeral and not self._config.allowlist.permits(self.guild_id, self.channel_id):
            raise RuntimeError("refusing to post outside the allowlist")
        mentions = discord.AllowedMentions.none()
        chunks = split_discord_text(content) or ["…"]
        first, *rest = chunks
        if self._interaction.response.is_done():
            await self._interaction.followup.send(
                first,
                ephemeral=ephemeral,
                allowed_mentions=mentions,
            )
        else:
            await self._interaction.response.send_message(
                first,
                ephemeral=ephemeral,
                allowed_mentions=mentions,
            )
        for chunk in rest:
            await self._interaction.followup.send(
                chunk,
                ephemeral=ephemeral,
                allowed_mentions=mentions,
            )

    async def shutdown(self) -> None:
        client = self._interaction.client
        close = getattr(client, "close", None)
        if close is not None:
            await close()


class EphyDiscordBot(discord.Client):
    def __init__(self, config: AppConfig, service: AdapterService) -> None:
        super().__init__(intents=build_intents())
        self.config = config
        self.service = service
        self.approvals = ApprovalQueue(config, service.store)
        self._mcp_runner = None
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(build_command_group(self))

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.config.guild_id)
        self.tree.copy_global_to(guild=guild)
        try:
            await self.tree.sync(guild=guild)
        except discord.HTTPException:
            log.error("slash command sync failed")
        for action in self.approvals.pending():
            self.add_view(build_approval_view(self, action["id"]))
        await self._maybe_start_mcp()

    async def _maybe_start_mcp(self) -> None:
        secrets = load_mcp_secrets()
        if not mcp_can_listen(self.config, secrets):
            if self.config.mcp.enabled:
                log.error("mcp is enabled but no bearer token or oauth secret is set")
            return
        app = McpApp(self.config, self.service, secrets)
        self._mcp_runner = await serve_mcp(app, self.config.mcp.bind_host, self.config.mcp.port)
        log.info("mcp listening on %s:%s", self.config.mcp.bind_host, self.config.mcp.port)

    async def on_message(self, message: discord.Message) -> None:
        await handle_incoming_message(
            message, config=self.config, service=self.service, bot_user=self.user
        )

    async def close(self) -> None:
        if self._mcp_runner is not None:
            await self._mcp_runner.cleanup()
        await self.service.aclose()
        await super().close()


def build_command_group(bot: EphyDiscordBot) -> app_commands.Group:
    group = app_commands.Group(name="ephy", description="ローカルのエフィに質問する")

    @group.command(name="ask", description="ローカルモデルに質問する")
    @app_commands.describe(question="質問文")
    async def ask(interaction: discord.Interaction, question: str) -> None:
        await slash_ask(interaction, question, config=bot.config, service=bot.service)

    @group.command(name="forget", description="自分の保存データを削除する")
    async def forget(interaction: discord.Interaction) -> None:
        await slash_forget(interaction, config=bot.config, service=bot.service)

    @group.command(name="purge", description="保存データを管理者が削除する")
    async def purge(interaction: discord.Interaction) -> None:
        await slash_purge(interaction, config=bot.config, service=bot.service)

    @group.command(name="stop", description="ボットを停止する")
    async def stop(interaction: discord.Interaction) -> None:
        await slash_stop(interaction, config=bot.config, service=bot.service)

    @group.command(name="propose", description="返信または未実装の操作を承認待ちにする")
    @app_commands.describe(text="承認後に扱う本文", kind="reply は返信，stub は記録だけ")
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="返信", value=REAL_KIND),
            app_commands.Choice(name="未実装の操作", value=STUB_KIND),
        ]
    )
    async def propose(
        interaction: discord.Interaction,
        text: str,
        kind: app_commands.Choice[str] | None = None,
    ) -> None:
        selected = kind.value if kind is not None else REAL_KIND
        await slash_propose(interaction, text, selected, bot=bot)

    return group


async def handle_incoming_message(
    message: discord.Message,
    *,
    config: AppConfig,
    service: AdapterService,
    bot_user: discord.abc.User | None,
) -> None:
    if bot_user is None or message.author.bot or message.author.id == bot_user.id:
        return
    if message.guild is None:
        return
    mentioned = any(getattr(item, "id", None) == bot_user.id for item in message.mentions)
    if not mentioned:
        return
    if not config.allowlist.permits(message.guild.id, message.channel.id):
        return
    question = extract_question(message.content or "", bot_user.id)
    await handle_ask(MentionResponder(message, config), service, question)


async def slash_ask(
    interaction: discord.Interaction,
    question: str,
    *,
    config: AppConfig,
    service: AdapterService,
) -> None:
    if interaction.guild_id != config.guild_id:
        await interaction.response.send_message(CHANNEL_DENIED, ephemeral=True)
        return
    if not config.allowlist.permits(interaction.guild_id, interaction.channel_id):
        await interaction.response.send_message(CHANNEL_DENIED, ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    await handle_ask(InteractionResponder(interaction, config), service, question)


async def slash_forget(
    interaction: discord.Interaction,
    *,
    config: AppConfig,
    service: AdapterService,
) -> None:
    await handle_forget(InteractionResponder(interaction, config), service)


async def slash_purge(
    interaction: discord.Interaction,
    *,
    config: AppConfig,
    service: AdapterService,
) -> None:
    await handle_purge(InteractionResponder(interaction, config), service)


async def slash_stop(
    interaction: discord.Interaction,
    *,
    config: AppConfig,
    service: AdapterService,
) -> None:
    await handle_stop(InteractionResponder(interaction, config), service)


async def slash_propose(
    interaction: discord.Interaction,
    text: str,
    kind: str,
    *,
    bot: EphyDiscordBot,
) -> None:
    config = bot.config
    if interaction.guild_id != config.guild_id or not config.allowlist.permits(
        interaction.guild_id, interaction.channel_id
    ):
        await interaction.response.send_message(CHANNEL_DENIED, ephemeral=True)
        return
    if not config.approver_user_ids:
        await interaction.response.send_message(NO_APPROVERS, ephemeral=True)
        return
    decision = bot.service.store.consume_rate(str(interaction.user.id), str(interaction.channel_id))
    if not decision.allowed:
        await interaction.response.send_message(
            "少し待ってからもう一度提案してください．",
            ephemeral=True,
        )
        return
    payload = {"text": text} if kind == REAL_KIND else {"name": "outbound", "text": text}
    action = bot.approvals.propose(
        guild_id=interaction.guild_id or 0,
        channel_id=interaction.channel_id or 0,
        requester_user_id=interaction.user.id,
        kind=kind,
        payload=payload,
    )
    bot.service.audit.write(
        {
            "ts": format_time(bot.service.store.clock.now()),
            "event": "action_proposed",
            "user_id": str(interaction.user.id),
            "channel_id": str(interaction.channel_id),
            "guild_id": str(interaction.guild_id),
            "action_id": action["id"],
            "action_kind": kind,
        }
    )
    await interaction.response.send_message(
        "承認を待っています．承認者だけがボタンを押せます．",
        view=build_approval_view(bot, action["id"]),
    )


def build_approval_view(bot: EphyDiscordBot, action_id: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)

    async def on_approve(interaction: discord.Interaction) -> None:
        await press_approval(bot, interaction, action_id, "approved")

    async def on_reject(interaction: discord.Interaction) -> None:
        await press_approval(bot, interaction, action_id, "rejected")

    approve = discord.ui.Button(
        label="承認",
        style=discord.ButtonStyle.success,
        custom_id=f"ephy:approve:{action_id}",
    )
    reject = discord.ui.Button(
        label="却下",
        style=discord.ButtonStyle.danger,
        custom_id=f"ephy:reject:{action_id}",
    )
    approve.callback = on_approve
    reject.callback = on_reject
    view.add_item(approve)
    view.add_item(reject)
    return view


async def press_approval(
    bot: EphyDiscordBot,
    interaction: discord.Interaction,
    action_id: str,
    decision: str,
) -> None:
    result = bot.approvals.decide(action_id, interaction.user.id, decision)
    bot.service.audit.write(
        {
            "ts": format_time(bot.service.store.clock.now()),
            "event": "action_decided",
            "user_id": str(interaction.user.id),
            "channel_id": str(interaction.channel_id or ""),
            "guild_id": str(interaction.guild_id or ""),
            "action_id": action_id,
            "action_kind": result.kind,
            "decision": result.status,
        }
    )
    if (
        result.status == "approved"
        and result.kind == REAL_KIND
        and result.reply_text
        and interaction.channel is not None
        and bot.config.allowlist.permits(interaction.guild_id, interaction.channel.id)
    ):
        await interaction.channel.send(
            result.reply_text,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    if interaction.response.is_done():
        await interaction.followup.send(result.text, ephemeral=result.status == "forbidden")
    else:
        await interaction.response.send_message(result.text, ephemeral=result.status == "forbidden")


def intent_value(intents: discord.Intents) -> int:
    return int(intents.value)


def expected_intent_value() -> int:
    return INTENT_GUILDS | INTENT_GUILD_MESSAGES | INTENT_MESSAGE_CONTENT


def _actor_permissions(interaction: discord.Interaction) -> object:
    user_permissions = getattr(interaction.user, "guild_permissions", None)
    if user_permissions is not None:
        return user_permissions
    return getattr(interaction, "permissions", None)


def command_names(group: app_commands.Group) -> list[str]:
    return sorted(command.name for command in group.commands)

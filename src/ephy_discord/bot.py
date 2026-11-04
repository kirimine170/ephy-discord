"""discord.py adapter. Outbound traffic is a reply in an allowed channel."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ephy_discord.config import AppConfig
from ephy_discord.handlers import (
    CHANNEL_DENIED,
    handle_ask,
    handle_forget,
    handle_purge,
    handle_stop,
    is_actor_admin,
)
from ephy_discord.permissions import (
    INTENT_GUILD_MESSAGES,
    INTENT_GUILDS,
    INTENT_MESSAGE_CONTENT,
)
from ephy_discord.service import AdapterService
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
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(build_command_group(self))

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.config.guild_id)
        self.tree.copy_global_to(guild=guild)
        try:
            await self.tree.sync(guild=guild)
        except discord.HTTPException:
            log.error("slash command sync failed")

    async def on_message(self, message: discord.Message) -> None:
        await handle_incoming_message(
            message, config=self.config, service=self.service, bot_user=self.user
        )

    async def close(self) -> None:
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

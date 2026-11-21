"""Normal discord.py Gateway binding；no additional Bot credential or webhook identity．"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.http import Route

from ephy_discord.parliament import RESULT_PREFIX, Event, Parliament, ParliamentError, load_policy
from ephy_discord.parliament_executor import RegisteredBackend, clean

log = logging.getLogger(__name__)


class DiscordDirectory:
    def __init__(self, client):
        self.client = client

    async def channel(self, identifier):
        return await self.client.http.get_channel(identifier)

    async def guild(self, identifier):
        return await self.client.http.get_guild(identifier)

    async def member(self, guild, identifier):
        # Raw REST role IDs are authoritative；cached Member.roles is not consulted．
        return await self.client.http.get_member(guild, identifier)

    async def roles(self, guild):
        return await self.client.http.get_roles(guild)

    async def message(self, channel, identifier):
        return await self.client.http.get_message(channel, identifier)


class ParliamentGateway:
    def __init__(self, client, config):
        self.client = client
        self.policy = load_policy(config.parliament_policy)
        if self.policy is None:
            raise ParliamentError("disabled_parliament_policy")
        if (
            self.policy.guild != config.guild_id
            or self.policy.channel not in config.allowed_channel_ids
        ):
            raise ParliamentError("parliament_outside_existing_allowlist")
        directory = DiscordDirectory(client)
        self.engine = Parliament(self.policy, directory, RegisteredBackend(directory))
        self.ready = False
        self.task = None
        self.active = set()

    async def start(self):
        user = await self.client.http.request(Route("GET", "/users/@me"))
        app = await self.client.application_info()
        identity = self.policy.bots[self.policy.self_name]
        if (
            int(user["id"]) != identity["user_id"]
            or user.get("bot") is not True
            or app.id != identity["application_id"]
            or self.client.user is None
            or self.client.user.id != identity["user_id"]
        ):
            raise ParliamentError("authenticated_bot_identity_mismatch")
        self.engine.fresh()
        self.ready = True
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.recover())

    def owns_channel(self, message) -> bool:
        return (
            message.guild is not None
            and message.guild.id == self.policy.guild
            and (
                message.channel.id == self.policy.channel
                or getattr(message.channel, "parent_id", None) == self.policy.channel
            )
        )

    async def handle(self, message) -> bool:
        if not self.owns_channel(message):
            return False
        if not self.ready:
            return True
        current = asyncio.current_task()
        self.active.add(current)
        event = Event(
            message.id,
            message.guild.id,
            message.channel.id,
            message.author.id,
            message.author.bot,
            message.content or "",
            message.reference.message_id if message.reference else None,
            message.webhook_id is not None,
            message.is_system(),
            message.mention_everyone,
        )
        try:
            job = await self.engine.admit(event)
            if job is not None:
                await self.engine.run(job, self.send)
        except Exception as exc:
            # Do not echo untrusted input，HTTP bodies，credentials or participant text．
            log.warning("parliament_event_rejected:%s", type(exc).__name__)
        finally:
            self.active.discard(current)
        return True

    async def send(self, request, job, result):
        event = request.event
        channel = await self.client.fetch_channel(event.channel)
        if channel.guild.id != event.guild or channel.id != event.channel:
            raise ParliamentError("delivery_route_changed")
        message = await channel.fetch_message(event.message)
        if (
            message.guild.id != event.guild
            or message.channel.id != event.channel
            or message.author.id != event.author
            or message.author.bot != event.author_bot
            or message.webhook_id is not None
        ):
            raise ParliamentError("delivery_requester_changed")
        result = clean(result)
        # One bounded message makes ambiguous delivery non-replayable as a unit．
        encoded = result.encode("utf-16-le")
        if len(encoded) > 2000:
            result = (
                encoded[:2000].decode("utf-16-le", errors="ignore")
                + "\n（長文のため表示を省略しました．結果全文はprivate ledgerに保管しています．）"
            )
        result = discord.utils.escape_mentions(result)
        recipients = [event.author, *request.notify]
        content = (
            f"{RESULT_PREFIX}job={job}]\n"
            + " ".join(f"<@{identifier}>" for identifier in dict.fromkeys(recipients))
            + "\n"
            + f"status={self.engine.ledger.get(job)['execution']}\n"
            + result
        )
        response = await message.reply(
            content,
            mention_author=False,
            suppress_embeds=True,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                roles=False,
                replied_user=False,
                users=[discord.Object(identifier) for identifier in recipients],
            ),
        )
        return response.id

    async def recover(self):
        while True:
            for job in self.engine.ledger.pending():
                try:
                    await self.engine.run(job, self.send)
                except Exception as exc:
                    log.warning("parliament_recovery_rejected:%s", type(exc).__name__)
            await asyncio.sleep(2)

    async def close(self):
        self.ready = False
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        active = [task for task in self.active if task is not asyncio.current_task()]
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ephy_discord.parliament import (
    RESULT_PREFIX,
    Event,
    Parliament,
    ParliamentError,
    Request,
    load_policy,
    parse,
)
from ephy_discord.parliament_discord import DiscordDirectory, ParliamentGateway
from ephy_discord.parliament_executor import RegisteredBackend


class Directory:
    def __init__(self):
        self.role = True
        self.exists = True
        self.bot = False
        self.calls = []
        self.wrong_channel = False
        self.wrong_member = False

    async def guild(self, identifier):
        self.calls.append("guild")
        return {"id": identifier}

    async def channel(self, identifier):
        self.calls.append("channel")
        return {
            "id": identifier,
            "guild_id": 2 if self.wrong_channel else 1,
            "type": 11 if identifier == 11 else 0,
            "parent_id": 10,
        }

    async def member(self, guild, identifier):
        self.calls.append("member")
        if not self.exists:
            raise RuntimeError("synthetic_member_missing")
        return {
            "user": {
                "id": 999 if self.wrong_member else identifier,
                "bot": self.bot if identifier == 100 else False,
            },
            "roles": [900] if self.role else [],
        }

    async def roles(self, guild):
        self.calls.append("roles")
        return [{"id": 900}]

    async def message(self, channel, identifier):
        return {"id": identifier, "channel_id": channel, "author": {"bot": True}}


class Backend:
    def __init__(self):
        self.calls = []
        self.error = None

    async def execute(self, request, policy, job_id):
        self.calls.append((request, job_id))
        if self.error:
            raise self.error
        await asyncio.sleep(0)
        return "Synthetic proposal．"


@pytest.fixture
def setup(tmp_path):
    source = tmp_path / "registered-repo"
    source.mkdir()
    value = {
        "version": 1,
        "enabled": True,
        "guild_id": 1,
        "channel_id": 10,
        "developer_role_id": 900,
        "self_bot": "Codex",
        "bots": {
            name: {
                "user_id": identifier,
                "application_id": identifier + 1000,
                "conversation_url": "",
                "backend": "codex" if name == "Codex" else "existing",
            }
            for name, identifier in zip(
                ("HackSick", "ESSEN", "Dot", "Codex"), (100, 200, 300, 400), strict=True
            )
        },
        "repositories": {
            "demo": {
                "path": str(source),
                "member_ids": [100, 500],
                "recipient_bots": ["Codex"],
                "allowed_paths": ["src/"],
                "github_repository": "synthetic/parliament",
                "base_branch": "main",
                "checks": [
                    {
                        "argv": [str(Path(sys.executable).resolve()), "-m", "pytest"],
                        "sha256": "0" * 64,
                    }
                ],
            }
        },
        "state_dir": str(tmp_path / "private-state"),
        "codex": {"enabled": False},
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    policy = load_policy(path)
    directory, backend = Directory(), Backend()
    engine = Parliament(policy, directory, backend)
    event = Event(10000, 1, 11, 500, False, "<@400> implement demo\nSynthetic change")
    return SimpleNamespace(
        path=path,
        value=value,
        policy=policy,
        directory=directory,
        backend=backend,
        engine=engine,
        event=event,
    )


@pytest.mark.asyncio
async def test_fresh_roles_repository_checks_and_same_thread_delivery(setup):
    x = setup
    job = await x.engine.admit(x.event)
    send = AsyncMock(return_value=20000)
    await x.engine.run(job, send)
    assert x.directory.calls.count("roles") == 2
    assert x.directory.calls.count("member") == 3
    assert len(x.backend.calls) == 1
    assert send.call_args.args[0].event.channel == 11
    assert send.call_args.args[0].event.author == 500
    assert x.engine.ledger.get(job)["delivery"] == "sent"
    assert x.engine.ledger.own_reply(replace(x.event, reply_to=20000), 400)
    assert not x.engine.ledger.own_reply(replace(x.event, channel=12, reply_to=20000), 400)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["role", "membership", "repository_policy", "disable"])
async def test_revocation_between_admission_and_execution_fails_closed(setup, change):
    x = setup
    job = await x.engine.admit(x.event)
    if change == "role":
        x.directory.role = False
    elif change == "membership":
        x.directory.exists = False
    elif change == "repository_policy":
        x.value["repositories"]["demo"]["member_ids"] = [100]
        x.path.write_text(json.dumps(x.value), encoding="utf-8")
    else:
        x.path.write_text('{"version":1,"enabled":false}', encoding="utf-8")
    try:
        await x.engine.run(job, AsyncMock(return_value=20000))
    except (ParliamentError, RuntimeError):
        pass
    assert x.backend.calls == []
    assert x.engine.ledger.get(job)["execution"] in {"failed", "unknown"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "role",
        "repo",
        "channel",
        "member",
        "webhook",
        "system",
        "everyone",
        "self",
        "unregistered_bot",
    ],
)
async def test_no_authority_from_names_quotes_or_invalid_origin(setup, change):
    x, event = setup, setup.event
    if change == "role":
        x.directory.role = False
    elif change == "repo":
        event = replace(event, content="<@400> implement other\nSynthetic change")
    elif change == "channel":
        x.directory.wrong_channel = True
    elif change == "member":
        x.directory.wrong_member = True
    elif change in {"webhook", "system", "everyone"}:
        event = replace(event, **{change: True})
    elif change == "self":
        event = replace(event, author=400, author_bot=True)
    else:
        event = replace(
            event,
            author=777,
            author_bot=True,
            content="<@400> implement demo request=synthetic01 hop=0\nSynthetic change",
        )
        x.directory.bot = True
    try:
        assert await x.engine.admit(event) is None
    except ParliamentError:
        pass
    assert x.engine.ledger.pending() == []
    assert x.backend.calls == []


@pytest.mark.asyncio
async def test_member_questions_and_replies_need_no_developer_role(setup):
    x = setup
    x.directory.role = False
    event = replace(x.event, content="<@400> ask\nSynthetic question")
    first = await x.engine.admit(event)
    await x.engine.run(first, AsyncMock(return_value=20000))
    reply = replace(event, message=10001, author=600, content="Synthetic follow-up", reply_to=20000)
    second = await x.engine.admit(reply)
    await x.engine.run(second, AsyncMock(return_value=20001))
    assert len(x.backend.calls) == 2
    assert x.backend.calls[1][0].kind == "ask"
    assert x.backend.calls[1][0].event.channel == 11
    assert x.directory.calls.count("roles") == 0


@pytest.mark.asyncio
async def test_message_dedup_parallel_execution_and_changed_replay(setup):
    x = setup
    jobs = await asyncio.gather(x.engine.admit(x.event), x.engine.admit(x.event))
    assert sum(job is not None for job in jobs) == 1
    job = next(job for job in jobs if job)
    send = AsyncMock(return_value=20000)
    await asyncio.gather(x.engine.run(job, send), x.engine.run(job, send))
    assert len(x.backend.calls) == 1
    assert send.await_count == 1
    with pytest.raises(ParliamentError, match="duplicate_message_changed"):
        await x.engine.admit(replace(x.event, content="<@400> implement demo\nChanged"))


@pytest.mark.asyncio
async def test_registered_bot_requires_explicit_envelope_and_cannot_loop(setup):
    x = setup
    x.directory.bot = True
    event = replace(x.event, author=100, author_bot=True)
    assert await x.engine.admit(event) is None
    event = replace(event, content="<@400> implement demo request=synthetic01 hop=0\nSynthetic")
    job = await x.engine.admit(event)
    assert job is not None
    with pytest.raises(ParliamentError, match="duplicate_message_changed"):
        await x.engine.admit(replace(event, message=10001))
    assert (
        await x.engine.admit(replace(event, message=10002, content=RESULT_PREFIX + "Synthetic"))
        is None
    )
    assert (
        await x.engine.admit(
            replace(event, message=10003, content=event.content.replace("hop=0", "hop=1"))
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["execution", "delivery"])
async def test_uncertain_outcomes_are_not_replayed_after_restart(setup, phase):
    x = setup
    job = await x.engine.admit(x.event)
    send = AsyncMock(return_value=20000)
    if phase == "execution":
        x.backend.error = RuntimeError("synthetic_unknown")
    else:
        send.side_effect = RuntimeError("synthetic_send_unknown")
    try:
        await x.engine.run(job, send)
    except ParliamentError:
        pass
    restarted = Parliament(x.policy, x.directory, x.backend)
    assert restarted.ledger.pending() == []
    await restarted.run(job, send)
    assert len(x.backend.calls) == 1
    assert send.await_count == (1 if phase == "delivery" else 0)


@pytest.mark.asyncio
async def test_pending_admission_can_resume_with_fresh_authorization(setup):
    x = setup
    job = await x.engine.admit(x.event)
    restarted = Parliament(x.policy, x.directory, x.backend)
    assert restarted.ledger.pending() == [job]
    await restarted.run(job, AsyncMock(return_value=20000))
    assert len(x.backend.calls) == 1


@pytest.mark.parametrize(
    "content",
    [
        "> <@400> implement demo\nquoted",
        "<@999> implement demo\nno",
        "<@400> implement\nno",
        "<@400> ask demo\nno",
        "<@400> implement demo extra\nno",
    ],
)
def test_only_explicit_header_requests_are_authoritative(setup, content):
    assert parse(replace(setup.event, content=content), setup.policy, own_reply=False) is None


@pytest.mark.asyncio
async def test_individual_human_notifications_are_explicit_and_validated(setup):
    x = setup
    event = replace(x.event, content="<@400> ask notify=<@600>,<@601>\nSynthetic")
    job = await x.engine.admit(event)
    assert x.engine.ledger.get(job)["execution"] == "accepted"
    assert x.directory.calls.count("member") == 3
    member = x.directory.member

    async def bot_notification(guild, identifier):
        data = await member(guild, identifier)
        if identifier == 600:
            data["user"]["bot"] = True
        return data

    x.directory.member = bot_notification
    with pytest.raises(ParliamentError, match="human_notification_denied"):
        await x.engine.admit(replace(event, message=10001))


def test_strict_policy_and_distinct_real_bot_registry(setup):
    x = setup
    assert load_policy(x.path).self_id == 400
    x.value["bots"]["Dot"]["user_id"] = 400
    x.path.write_text(json.dumps(x.value), encoding="utf-8")
    with pytest.raises(ParliamentError, match="shared_bot_identity"):
        load_policy(x.path)
    x.path.write_text('{"version":1,"enabled":false}', encoding="utf-8")
    assert load_policy(x.path) is None
    x.path.write_text('{"version":1,"version":1,"enabled":false}', encoding="utf-8")
    with pytest.raises(ParliamentError, match="duplicate_policy_key"):
        load_policy(x.path)


@pytest.mark.asyncio
async def test_sdk_directory_uses_rest_not_member_or_role_caches():
    http = SimpleNamespace(
        **{
            name: AsyncMock(return_value={})
            for name in ("get_channel", "get_guild", "get_member", "get_roles")
        }
    )
    directory = DiscordDirectory(SimpleNamespace(http=http))
    await directory.guild(1)
    await directory.channel(11)
    await directory.member(1, 500)
    await directory.roles(1)
    http.get_member.assert_awaited_once_with(1, 500)
    http.get_roles.assert_awaited_once_with(1)


def gateway(setup):
    x = setup
    client = SimpleNamespace(
        user=SimpleNamespace(id=400),
        http=SimpleNamespace(request=AsyncMock(return_value={"id": "400", "bot": True})),
        application_info=AsyncMock(return_value=SimpleNamespace(id=1400)),
    )
    config = SimpleNamespace(parliament_policy=x.path, guild_id=1, allowed_channel_ids={10})
    return ParliamentGateway(client, config), client


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["none", "user", "application", "not_bot"])
async def test_startup_requires_actual_authenticated_user_and_application(setup, bad):
    bridge, client = gateway(setup)
    if bad == "user":
        client.http.request.return_value["id"] = "999"
    elif bad == "application":
        client.application_info.return_value.id = 999
    elif bad == "not_bot":
        client.http.request.return_value["bot"] = False
    if bad == "none":
        await bridge.start()
        assert bridge.ready
        await bridge.close()
    else:
        with pytest.raises(ParliamentError, match="authenticated_bot_identity"):
            await bridge.start()
        assert bridge.task is None


@pytest.mark.asyncio
async def test_result_replies_to_persisted_origin_and_only_mentions_explicit_people(setup):
    bridge, client = gateway(setup)
    original = SimpleNamespace(
        id=10000,
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=11),
        author=SimpleNamespace(id=500, bot=False),
        webhook_id=None,
        reply=AsyncMock(return_value=SimpleNamespace(id=20000)),
    )
    channel = SimpleNamespace(
        id=11, guild=SimpleNamespace(id=1), fetch_message=AsyncMock(return_value=original)
    )
    client.fetch_channel = AsyncMock(return_value=channel)
    request = Request(setup.event, "ask", None, "Synthetic", (600,))
    job = bridge.engine.ledger.accept(request, setup.policy)
    await bridge.send(request, job, "Do not ping @everyone or <@700> " + "😀" * 2000)
    kwargs = original.reply.call_args.kwargs
    content = original.reply.call_args.args[0]
    assert len(content.encode("utf-16-le")) // 2 <= 2000
    assert [person.id for person in kwargs["allowed_mentions"].users] == [500, 600]
    assert kwargs["allowed_mentions"].roles is False
    assert kwargs["mention_author"] is False
    assert content.startswith(RESULT_PREFIX)
    original.author.id = 999
    with pytest.raises(ParliamentError, match="delivery_requester_changed"):
        await bridge.send(request, job, "Synthetic")
    assert original.reply.await_count == 1


@pytest.mark.asyncio
async def test_disabled_codex_cannot_use_owner_repair_or_inherited_credentials(setup):
    request = Request(setup.event, "implement", "demo", "Synthetic")
    with pytest.raises(ParliamentError, match="codex_executor_disabled"):
        await RegisteredBackend(setup.directory).execute(request, setup.policy, "synthetic")


@pytest.mark.asyncio
async def test_normal_gateway_consumes_parliament_messages_before_legacy_logging(monkeypatch):
    from ephy_discord import bot as module

    bridge = SimpleNamespace(handle=AsyncMock(return_value=True))
    instance = SimpleNamespace(parliament=bridge)
    legacy = AsyncMock(side_effect=AssertionError("legacy model must not receive parliament input"))
    monkeypatch.setattr(module, "handle_incoming_message", legacy)
    monkeypatch.setattr(
        module, "observe_message", lambda *_: pytest.fail("must not log all messages")
    )
    message = object()
    await module.EphyDiscordBot.on_message(instance, message)
    bridge.handle.assert_awaited_once_with(message)
    legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_opt_in_gateway_never_syncs_commands_or_opens_shared_mcp(setup):
    from ephy_discord.bot import EphyDiscordBot
    from tests.conftest import make_service

    service, _, _ = make_service(setup.path.parent)
    config = replace(service.config, parliament_policy=setup.path)
    client = EphyDiscordBot(config, service)
    client.tree.sync = AsyncMock(side_effect=AssertionError("must not overwrite slash commands"))
    client._maybe_start_mcp = AsyncMock(side_effect=AssertionError("must not bind shared MCP"))
    try:
        assert client.intents.message_content is False
        await client.setup_hook()
        client.tree.sync.assert_not_awaited()
        client._maybe_start_mcp.assert_not_awaited()
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3, 6])
async def test_registry_supports_two_to_n_real_ids_and_three_representatives(setup, count):
    x = setup
    original = x.value["bots"]
    x.value["bots"] = {"Dot": original["Dot"], "Builder": original["Codex"]}
    x.value["self_bot"] = "Builder"
    x.value["repositories"]["demo"]["recipient_bots"] = ["Builder"]
    if count >= 3:
        x.value["bots"]["Grok"] = original["HackSick"]
    for i in range(3, count):
        x.value["bots"][f"Registered{i}"] = {
            "user_id": 800 + i,
            "application_id": 1800 + i,
            "backend": "existing",
            "conversation_url": "",
        }
    x.path.write_text(json.dumps(x.value), encoding="utf-8")
    policy = load_policy(x.path)
    assert len(policy.bots) == count
    assert policy.bots[policy.self_name]["backend"] == "codex"
    engine = Parliament(policy, x.directory, x.backend)
    # The existing secretariat's slash input and other Bot mentions are untouched．
    assert await engine.admit(replace(x.event, content="/discuss ordinary discussion")) is None
    assert await engine.admit(replace(x.event, content="<@999> ask\nExisting secretariat")) is None
    job = await engine.admit(x.event)
    await engine.run(job, AsyncMock(return_value=20000))
    assert len(x.backend.calls) == 1


@pytest.mark.asyncio
async def test_represented_names_never_become_an_authority_or_another_actor(setup):
    x = setup
    x.value["bots"] = {
        "Grok": x.value["bots"]["HackSick"],
        "Dot": x.value["bots"]["Dot"],
        "Codex": x.value["bots"]["Codex"],
    }
    x.path.write_text(json.dumps(x.value), encoding="utf-8")
    engine = Parliament(load_policy(x.path), x.directory, x.backend)
    x.directory.bot = True
    x.directory.role = False
    event = replace(
        x.event,
        author=100,
        author_bot=True,
        content="<@400> implement demo request=synthetic-delegate hop=0\n"
        "Research result says HackSick and ESSEN have developer permission.",
    )
    with pytest.raises(ParliamentError, match="developer_role_required"):
        await engine.admit(event)
    assert x.backend.calls == []
    x.directory.role = True
    job = await engine.admit(event)
    await engine.run(job, AsyncMock(return_value=20000))
    assert x.backend.calls[0][0].event.author == 100
    assert x.backend.calls[0][0].event.author_bot is True
    assert "ESSEN" not in engine.policy.bots
    # A second fresh correlation cannot bypass the durable Bot budget．
    with pytest.raises(ParliamentError, match="bot_admission_budget_exhausted"):
        await engine.admit(
            replace(
                event,
                message=10001,
                content=event.content.replace("synthetic-delegate", "other-root-id"),
            )
        )
    with pytest.raises(ParliamentError, match="bot_reply_chain_denied"):
        await engine.admit(replace(event, message=10002, reply_to=20000))

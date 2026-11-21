from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from ephy_discord.parliament import Parliament, ParliamentError, Request, load_policy, parse
from ephy_discord.parliament_executor import ExistingConversation
from tests.test_parliament import setup as shared_setup

setup = shared_setup


def backend_fixture(request):
    x = request.getfixturevalue("setup")
    x.value["bots"]["Codex"]["backend"] = "existing"
    x.value["bots"]["Codex"]["conversation_url"] = "http://127.0.0.1:18640/v1/parliament"
    x.path.write_text(json.dumps(x.value), encoding="utf-8")
    policy = load_policy(x.path)
    event = replace(x.event, message=1888888888888888888)
    job = "a" * 64
    identity = {
        "version": 1,
        "job_id": job,
        "guild_id": str(event.guild),
        "channel_id": str(event.channel),
        "requester_id": str(event.author),
        "recipient_id": str(policy.self_id),
        "kind": "ask",
        "message_id": str(event.message),
    }
    reply = {
        **identity,
        "status": "completed",
        "run_id": "run_gateway_" + job,
        "turn_id": "run_gateway_" + job + "_t0",
        "authenticated_answer": True,
        "discord_display": "posted",
        "message_url": f"https://discord.com/channels/{event.guild}/{event.channel}/20000",
    }
    return SimpleNamespace(
        policy=policy,
        request=Request(event, "ask", None, "Synthetic public question"),
        job=job,
        identity=identity,
        reply=reply,
    )


@pytest.mark.asyncio
async def test_existing_run_starts_once_polls_get_and_returns_only_delivery_receipt(request):
    x = backend_fixture(request)
    calls = []

    async def handler(req):
        calls.append(req)
        assert "authorization" not in req.headers and "cookie" not in req.headers
        if req.method == "POST":
            assert json.loads(req.content) == {**x.identity, "text": x.request.text}
            return httpx.Response(
                200,
                json={
                    **x.reply,
                    "status": "pending",
                    "authenticated_answer": False,
                    "discord_display": "pending",
                    "message_url": None,
                },
            )
        assert dict(req.url.params) == {"job_id": x.job}
        return httpx.Response(200, json=x.reply)

    async def sleep(_):
        pass

    result = await ExistingConversation(
        transport=httpx.MockTransport(handler), sleep=sleep
    ).execute(x.request, x.policy, x.job)
    assert [r.method for r in calls] == ["POST", "GET"]
    assert x.reply["message_url"] in result
    assert "同じスレッド" in result and x.request.text not in result
    assert json.loads(calls[0].content)["message_id"] == "1888888888888888888"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    [
        "requester_id",
        "recipient_id",
        "message_id",
        "channel_id",
        "numeric_id",
        "unobserved",
        "wrong_destination",
        "failed",
        "wrong_run",
    ],
)
async def test_existing_run_never_accepts_identity_or_completion_claim_mismatch(request, mismatch):
    x = backend_fixture(request)
    changed = dict(x.reply)
    if mismatch in x.identity:
        changed[mismatch] = "999"
    elif mismatch == "numeric_id":
        changed["recipient_id"] = x.policy.self_id
    elif mismatch == "unobserved":
        changed["authenticated_answer"] = False
    elif mismatch == "wrong_destination":
        changed["message_url"] = "https://discord.com/channels/1/999/20000"
    elif mismatch == "wrong_run":
        changed["run_id"] = "another-run"
    else:
        changed["status"] = "unavailable"
    calls = []

    def handler(req):
        calls.append(req.method)
        if mismatch == "wrong_run" and req.method == "POST":
            return httpx.Response(200, json={**x.reply, "status": "pending"})
        return httpx.Response(200, json=changed)

    async def sleep(_):
        pass

    with pytest.raises(ParliamentError):
        await ExistingConversation(transport=httpx.MockTransport(handler), sleep=sleep).execute(
            x.request, x.policy, x.job
        )
    assert calls.count("POST") == 1


@pytest.mark.asyncio
async def test_ambiguous_start_and_pending_deadline_never_repeat_post(request):
    x = backend_fixture(request)
    calls = []

    def ambiguous(req):
        calls.append(req.method)
        raise httpx.ReadTimeout("synthetic ambiguous acceptance")

    with pytest.raises(httpx.ReadTimeout):
        await ExistingConversation(transport=httpx.MockTransport(ambiguous)).execute(
            x.request, x.policy, x.job
        )
    assert calls == ["POST"]
    clock = SimpleNamespace(now=0)

    async def sleep(_):
        clock.now += 101

    def pending(req):
        calls.append(req.method)
        return httpx.Response(
            200,
            json={
                **x.reply,
                "status": "pending",
                "authenticated_answer": False,
                "discord_display": "pending",
                "message_url": None,
            },
        )

    calls.clear()
    with pytest.raises(RuntimeError, match="unconfirmed_no_retry"):
        await ExistingConversation(
            transport=httpx.MockTransport(pending), clock=lambda: clock.now, sleep=sleep
        ).execute(x.request, x.policy, x.job)
    assert calls == ["POST", "GET"]


@pytest.mark.asyncio
async def test_transient_get_rate_limit_or_restart_does_not_repeat_start(request):
    x = backend_fixture(request)
    calls = []

    async def handler(req):
        calls.append(req.method)
        if req.method == "POST":
            return httpx.Response(200, json={**x.reply, "status": "pending"})
        if len(calls) == 2:
            return httpx.Response(503, json={"error": "OUTBOX_BACKOFF"})
        if len(calls) == 3:
            raise httpx.ReadTimeout("synthetic service restart")
        return httpx.Response(200, json=x.reply)

    async def sleep(_):
        pass

    result = await ExistingConversation(
        transport=httpx.MockTransport(handler), sleep=sleep
    ).execute(x.request, x.policy, x.job)
    assert x.reply["message_url"] in result
    assert calls == ["POST", "GET", "GET", "GET"]


@pytest.mark.asyncio
async def test_plain_reply_uses_verified_relay_provenance_and_never_another_recipient(request):
    x = backend_fixture(request)
    event = replace(x.request.event, content="Synthetic follow-up", reply_to=20000)

    def handler(req):
        assert req.method == "GET"
        assert dict(req.url.params) == {
            "channel_id": str(event.channel),
            "recipient_id": str(x.policy.self_id),
            "reply_to": "20000",
        }
        return httpx.Response(
            200,
            json={
                "version": 1,
                "guild_id": str(event.guild),
                "channel_id": str(event.channel),
                "recipient_id": str(x.policy.self_id),
                "reply_to": "20000",
                "owned": True,
            },
        )

    backend = ExistingConversation(transport=httpx.MockTransport(handler))
    assert await backend.owns_reply(event, x.policy) is True
    original = request.getfixturevalue("setup")
    engine = Parliament(x.policy, original.directory, backend)
    job = await engine.admit(event)
    assert engine.ledger.get(job)["execution"] == "accepted"
    assert (
        parse(replace(event, content="<@999> ask\nOther recipient"), x.policy, own_reply=True)
        is None
    )
    assert engine.ledger.own_reply(event, x.policy.self_id) is False

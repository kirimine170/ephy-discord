from __future__ import annotations

import json

import pytest

from ephy_discord.config import McpConfig
from ephy_discord.mcp import McpApp, McpSecrets, _pkce, mcp_can_listen
from tests.conftest import make_config, make_service


def _app(tmp_path, *, bearer="static-token", oauth_secret="owner-secret"):
    from dataclasses import replace

    service, llm, karte = make_service(tmp_path)
    service.config = replace(
        service.config,
        mcp=McpConfig(
            enabled=True,
            bind_host="127.0.0.1",
            port=8791,
            public_base_url="http://127.0.0.1:8791",
            token_ttl_seconds=3600,
        ),
    )
    app = McpApp(service.config, service, McpSecrets(bearer, oauth_secret))
    return app, service, llm, karte


def test_mcp_is_disabled_until_enabled_with_a_secret(tmp_path) -> None:
    config = make_config(tmp_path)
    assert config.mcp.enabled is False
    assert mcp_can_listen(config, McpSecrets("", "")) is False
    enabled = make_config(tmp_path)
    from dataclasses import replace

    enabled = replace(
        enabled,
        mcp=McpConfig(True, "127.0.0.1", 8791, "", 3600),
    )
    assert mcp_can_listen(enabled, McpSecrets("", "")) is False
    assert mcp_can_listen(enabled, McpSecrets("token", "")) is True


@pytest.mark.asyncio
async def test_mcp_tools_require_bearer_and_do_not_write_karte(tmp_path) -> None:
    app, service, llm, karte = _app(tmp_path)
    service.store.remember(
        guild_id="1",
        channel_id="10",
        user_id="7",
        discord_message_id=None,
        query_text="許可チャンネルの質問",
        answer_text="許可チャンネルの回答",
        digest="abc",
        model="mock",
        sources_json="[]",
        latency_ms=1,
        cache_hit=False,
    )
    service.store.remember(
        guild_id="1",
        channel_id="99",
        user_id="7",
        discord_message_id=None,
        query_text="許可外の質問",
        answer_text="許可外の回答",
        digest="def",
        model="mock",
        sources_json="[]",
        latency_ms=1,
        cache_hit=False,
    )
    denied = await app.handle("POST", "/mcp", {}, b"{}")
    assert denied.status == 401
    assert "oauth-protected-resource" in denied.headers["WWW-Authenticate"]

    headers = {"Authorization": "Bearer static-token"}
    listed = await app.handle(
        "POST",
        "/mcp",
        headers,
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
    )
    names = [tool["name"] for tool in json.loads(listed.body)["result"]["tools"]]
    assert names == ["karte_search", "karte_read", "discord_summary", "ask_ephy", "submit_sources"]

    summary = await _call(app, headers, "discord_summary", {})
    assert "許可チャンネルの質問" in summary
    assert "許可外の質問" not in summary

    search = await _call(app, headers, "karte_search", {"query": "境界"})
    assert "unavailable" in search or "ok" in search
    assert karte.queries == ["境界"]

    answer = await _call(app, headers, "ask_ephy", {"question": "自己紹介して"})
    assert "答えです" in answer
    assert llm.calls

    before = list(karte.queries)
    submitted = await _call(
        app,
        headers,
        "submit_sources",
        {
            "origin": "grok",
            "text": "SNS finding",
            "urls": ["https://example.com/post/1"],
        },
    )
    assert "written_to_karte" in submitted
    assert "false" in submitted
    assert karte.queries == before
    assert "SNS finding" in service.store.external_source_texts()
    raw = service.audit.path.read_text(encoding="utf-8")
    assert "static-token" not in raw
    assert "SNS finding" not in raw
    await service.aclose()


@pytest.mark.asyncio
async def test_oauth_pkce_issues_a_bearer_and_rejects_a_bad_verifier(tmp_path) -> None:
    app, service, _llm, _karte = _app(tmp_path, bearer="")
    registered = await app.handle(
        "POST",
        "/oauth/register",
        {},
        json.dumps({"redirect_uris": ["https://chatgpt.com/connector/oauth/callback"]}).encode(),
    )
    client = json.loads(registered.body)
    verifier = "a" * 50
    challenge = _pkce(verifier)
    form = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": client["redirect_uris"][0],
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
        "owner_secret": "owner-secret",
    }
    approved = await app.handle("POST", "/oauth/authorize", {}, _form(form))
    assert approved.status == 302
    location = approved.headers["Location"]
    code = location.split("code=", 1)[1].split("&", 1)[0]
    bad = await app.handle(
        "POST",
        "/oauth/token",
        {},
        _form(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": "b" * 50,
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "redirect_uri": client["redirect_uris"][0],
            }
        ),
    )
    assert bad.status == 400

    approved_again = await app.handle("POST", "/oauth/authorize", {}, _form(form))
    code = approved_again.headers["Location"].split("code=", 1)[1].split("&", 1)[0]
    good = await app.handle(
        "POST",
        "/oauth/token",
        {},
        _form(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "redirect_uri": client["redirect_uris"][0],
            }
        ),
    )
    token = json.loads(good.body)["access_token"]
    called = await app.handle(
        "POST",
        "/mcp",
        {"Authorization": f"Bearer {token}"},
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "discord_summary", "arguments": {}},
            }
        ).encode(),
    )
    assert called.status == 200
    metadata = await app.handle("GET", "/.well-known/oauth-authorization-server", {}, b"")
    body = json.loads(metadata.body)
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["registration_endpoint"].endswith("/oauth/register")
    await service.aclose()


def _form(values: dict) -> bytes:
    from urllib.parse import urlencode

    return urlencode(values).encode()


async def _call(app: McpApp, headers: dict, name: str, arguments: dict) -> str:
    response = await app.handle(
        "POST",
        "/mcp",
        headers,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        ).encode(),
    )
    payload = json.loads(response.body)
    return payload["result"]["content"][0]["text"]

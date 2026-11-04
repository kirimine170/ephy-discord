from __future__ import annotations

import json

import pytest

from ephy_discord.config import McpConfig
from ephy_discord.mcp import McpApp, McpSecrets, mcp_can_listen
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
    app = McpApp(service.config, service, McpSecrets(bearer, None, ""))
    return app, service, llm, karte


def test_mcp_is_disabled_until_enabled_with_a_secret(tmp_path) -> None:
    config = make_config(tmp_path)
    assert config.mcp.enabled is False
    assert mcp_can_listen(config, McpSecrets("", None, "")) is False
    from dataclasses import replace

    enabled = replace(config, mcp=McpConfig(True, "127.0.0.1", 8791, "", 3600))
    assert mcp_can_listen(enabled, McpSecrets("", None, "")) is False
    assert mcp_can_listen(enabled, McpSecrets("token", None, "")) is True


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
    assert denied.headers["WWW-Authenticate"] == "Bearer"
    health = await app.handle("GET", "/health", {}, b"")
    assert json.loads(health.body)["live_connectors"] is False
    foreign = await app.handle("GET", "/health", {"Host": "evil.example"}, b"")
    assert foreign.status == 400

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
async def test_connector_jwt_is_checked_with_pinned_keys(tmp_path) -> None:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    resource = "https://example.test/ephy"
    issuer = "https://issuer.example/realms/ephy"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "test"
    private = json.loads(RSAAlgorithm.to_jwk(key))
    assert "d" in private
    app, service, _llm, _karte = _app(tmp_path, bearer="")
    from ephy_discord.oauth_resource import OAuthSettings

    app.secrets = McpSecrets(
        "",
        OAuthSettings(issuer, resource, {"keys": [jwk]}),
        "",
    )
    import time

    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "connector-user",
            "iss": issuer,
            "aud": resource,
            "iat": now,
            "exp": now + 600,
            "scope": "ephy:read",
            "azp": "chatgpt",
        },
        key,
        algorithm="RS256",
        headers={"kid": "test"},
    )
    called = await app.handle(
        "POST",
        "/mcp",
        {"Authorization": f"Bearer {token}", "MCP-Protocol-Version": "2026-07-28"},
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            }
        ).encode(),
    )
    assert json.loads(called.body)["result"]["protocolVersion"] == "2026-07-28"
    missing_scope = jwt.encode(
        {
            "sub": "connector-user",
            "iss": issuer,
            "aud": resource,
            "iat": now,
            "exp": now + 600,
            "scope": "other",
            "azp": "chatgpt",
        },
        key,
        algorithm="RS256",
        headers={"kid": "test"},
    )
    rejected = await app.handle(
        "POST",
        "/mcp",
        {"Authorization": f"Bearer {missing_scope}"},
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}).encode(),
    )
    assert rejected.status == 401
    assert "/.well-known/oauth-protected-resource/mcp" in rejected.headers["WWW-Authenticate"]
    metadata = await app.handle("GET", "/.well-known/oauth-protected-resource/mcp", {}, b"")
    body = json.loads(metadata.body)
    assert body["authorization_servers"] == [issuer]
    assert body["scopes_supported"] == ["ephy:read"]
    from ephy_discord.oauth_resource import OAuthConfigurationError, load_oauth_settings

    try:
        load_oauth_settings(
            {
                "EPHY_MCP_OAUTH_ISSUER": issuer,
                "EPHY_MCP_OAUTH_AUDIENCE": resource,
                "EPHY_MCP_OAUTH_JWKS": json.dumps({"keys": [private]}),
            },
            resource_url=resource,
        )
    except OAuthConfigurationError:
        pass
    else:
        raise AssertionError("private jwk was accepted")
    await service.aclose()


@pytest.mark.asyncio
async def test_submit_sources_is_idempotent_and_https_only(tmp_path) -> None:
    app, service, _llm, karte = _app(tmp_path)
    headers = {"Authorization": "Bearer static-token"}
    first = await _call(
        app,
        headers,
        "submit_sources",
        {
            "origin": "chatgpt",
            "text": "finding",
            "urls": ["https://example.com/a"],
            "request_key": "same",
        },
    )
    second = await _call(
        app,
        headers,
        "submit_sources",
        {
            "origin": "chatgpt",
            "text": "finding",
            "urls": ["https://example.com/a"],
            "request_key": "same",
        },
    )
    assert json.loads(first)["source_id"] == json.loads(second)["source_id"]
    conflict = await _call(
        app,
        headers,
        "submit_sources",
        {
            "origin": "chatgpt",
            "text": "different",
            "urls": ["https://example.com/a"],
            "request_key": "same",
        },
    )
    assert conflict == "request_key_conflict"
    http_url = await _call(
        app,
        headers,
        "submit_sources",
        {"origin": "muse", "text": "no", "urls": ["http://example.com/a"]},
    )
    assert http_url == "urls must be https"
    assert karte.queries == []
    await service.aclose()


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

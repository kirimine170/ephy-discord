from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ephy_discord.channel_log import observe_delete, observe_edit, observe_message
from ephy_discord.mcp import McpApp, McpSecrets
from tests.conftest import ManualClock, make_service


def test_allowlisted_messages_are_cached_with_metadata(tmp_path) -> None:
    service, _llm, _karte = make_service(tmp_path)
    observe_message(_message("10", "hello", reply_to="9", bot=False), service.config, service.store)
    observe_message(
        _message("99", "secret", channel_id=99, author_id="8"),
        service.config,
        service.store,
    )
    observe_message(
        _message("11", "bot says", author_id="42", bot=True),
        service.config,
        service.store,
    )

    kept = service.store.channel_message("10")
    assert kept is not None
    assert kept["content"] == "hello"
    assert kept["author_id"] == "7"
    assert kept["display_name"] == "Member"
    assert kept["reply_to_id"] == "9"
    assert kept["is_bot"] == 0
    assert json.loads(kept["attachments_json"])[0]["url"] == "https://cdn.example/a.png"
    assert service.store.channel_message("99") is None
    bot = service.store.channel_message("11")
    assert bot is not None and bot["is_bot"] == 1
    service.store.close()


def test_edits_and_deletes_update_the_cache(tmp_path) -> None:
    service, _llm, _karte = make_service(tmp_path)
    observe_message(_message("10", "before"), service.config, service.store)
    observe_edit(
        SimpleNamespace(
            guild_id=1,
            channel_id=10,
            message_id=10,
            data={
                "content": "after",
                "edited_timestamp": "2026-11-04T01:00:00.000000Z",
                "attachments": [],
            },
        ),
        service.config,
        service.store,
    )
    edited = service.store.channel_message("10")
    assert edited is not None
    assert edited["content"] == "after"
    assert edited["edited_at"] is not None
    assert json.loads(edited["attachments_json"]) == []

    observe_delete(
        SimpleNamespace(guild_id=1, channel_id=10, message_id=10),
        service.config,
        service.store,
    )
    assert service.store.channel_message("10") is None
    observe_delete(
        SimpleNamespace(guild_id=1, channel_id=99, message_id=10),
        service.config,
        service.store,
    )
    service.store.close()


def test_retention_and_forget_remove_channel_content(tmp_path) -> None:
    clock = ManualClock(datetime(2026, 11, 4, tzinfo=UTC))
    service, _llm, _karte = make_service(tmp_path, clock)
    observe_message(
        _message("10", "old words", created=datetime(2026, 10, 1, tzinfo=UTC)),
        service.config,
        service.store,
    )
    observe_message(
        _message("12", "fresh words", author_id="8", created=datetime(2026, 11, 4, tzinfo=UTC)),
        service.config,
        service.store,
    )
    service.store.enforce_retention(vacuum=False)
    assert service.store.channel_message("10") is None
    assert service.store.channel_message("12")["content"] == "fresh words"

    service.forget(8)
    assert service.store.channel_message("12") is None
    service.store.upsert_channel_message(
        {
            "message_id": "30",
            "guild_id": "1",
            "channel_id": "10",
            "author_id": "3",
            "created_at": "2026-11-04T00:00:00.000000Z",
            "content": "purge me",
            "attachments_json": "[]",
            "is_bot": False,
        }
    )
    service.purge(1)
    assert service.store.channel_message("30") is None
    service.store.close()


def test_store_all_messages_can_be_disabled(tmp_path) -> None:
    from dataclasses import replace

    service, _llm, _karte = make_service(tmp_path)
    service.config = replace(
        service.config,
        cache=replace(service.config.cache, store_all_messages=False),
    )
    observe_message(_message("10", "skipped"), service.config, service.store)
    assert service.store.channel_message("10") is None
    service.store.close()


@pytest.mark.asyncio
async def test_read_tool_enforces_allowlist_and_limits(tmp_path) -> None:
    service, _llm, _karte = make_service(tmp_path, channels=(10, 11))
    for index in range(3):
        service.store.upsert_channel_message(
            {
                "message_id": str(100 + index),
                "guild_id": "1",
                "channel_id": "10",
                "author_id": "7",
                "display_name": "member",
                "created_at": f"2026-11-04T00:00:0{index}.000000Z",
                "content": f"line {index}",
                "reply_to_id": None,
                "attachments_json": "[]",
                "is_bot": False,
                "channel_name": "general",
            }
        )
    service.store.upsert_channel_message(
        {
            "message_id": "200",
            "guild_id": "1",
            "channel_id": "99",
            "author_id": "7",
            "created_at": "2026-11-04T00:00:09.000000Z",
            "content": "hidden",
            "attachments_json": "[]",
            "is_bot": False,
        }
    )
    app = McpApp(service.config, service, McpSecrets("static-token", None, ""))
    headers = {"Authorization": "Bearer static-token"}
    denied = await _call(app, headers, "discord_read_channel", {"channel_id": "99"})
    assert denied == "channel_not_allowed"
    listed = json.loads(await _call(app, headers, "discord_list_channels", {}))
    assert [item["channel_id"] for item in listed["channels"]] == ["10", "11"]
    assert listed["channels"][0]["name"] == "general"
    assert listed["untrusted"] is True
    page = json.loads(
        await _call(
            app,
            headers,
            "discord_read_channel",
            {"channel_id": "10", "limit": 2, "since": "2026-11-04T00:00:00Z"},
        )
    )
    assert page["untrusted"] is True
    assert [item["content"] for item in page["messages"]] == ["line 2", "line 1"]
    assert "hidden" not in json.dumps(page)
    too_many = await _call(app, headers, "discord_read_channel", {"limit": 201})
    assert too_many == "limit_out_of_range"
    before = json.loads(
        await _call(app, headers, "discord_read_channel", {"channel_id": "10", "before": "102"})
    )
    assert [item["message_id"] for item in before["messages"]] == ["101", "100"]
    summary = await _call(app, headers, "discord_summary", {})
    assert "count=3" in summary
    assert "hidden" not in summary
    assert "未信頼" in summary
    await service.aclose()


def _message(
    message_id: str,
    content: str,
    *,
    channel_id: int = 10,
    author_id: str = "7",
    reply_to: str | None = None,
    bot: bool = False,
    created: datetime | None = None,
):
    return SimpleNamespace(
        id=int(message_id) if message_id.isdigit() else message_id,
        content=content,
        created_at=created or datetime(2026, 11, 4, tzinfo=UTC),
        edited_at=None,
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=channel_id, name="general"),
        author=SimpleNamespace(id=int(author_id), bot=bot, display_name="Member"),
        reference=SimpleNamespace(message_id=int(reply_to)) if reply_to else None,
        attachments=[
            SimpleNamespace(
                id=1,
                url="https://cdn.example/a.png",
                filename="a.png",
                content_type="image/png",
                size=12,
            )
        ],
    )


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

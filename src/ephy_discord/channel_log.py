"""Cache ordinary messages from allowlisted channels. Content is untrusted data."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from ephy_discord.config import AppConfig
from ephy_discord.store import Store, format_precise

_MAX_CONTENT = 4000
_MAX_NAME = 80
_MAX_ATTACHMENTS = 10


def observe_message(message: object, config: AppConfig, store: Store) -> None:
    if not _enabled(config, _guild_id(message), _channel_id(message)):
        return
    created = _created_at(message)
    author_id = _author_id(message)
    if created is None or not author_id:
        return
    store.upsert_channel_message(_row_from_message(message, config, created, author_id))


def observe_edit(payload: object, config: AppConfig, store: Store) -> None:
    guild_id = getattr(payload, "guild_id", None)
    channel_id = getattr(payload, "channel_id", None)
    message_id = str(getattr(payload, "message_id", "") or "")
    if not message_id or not _enabled(config, guild_id, channel_id):
        return
    data = getattr(payload, "data", None)
    if not isinstance(data, dict):
        data = {}
    fields: dict = {}
    if "content" in data:
        fields["content"] = _clip(str(data.get("content") or ""), _MAX_CONTENT)
    if "attachments" in data:
        fields["attachments_json"] = json.dumps(
            _attachments(data.get("attachments")),
            ensure_ascii=False,
        )
    edited = data.get("edited_timestamp")
    if isinstance(edited, str) and edited:
        fields["edited_at"] = _normalize_timestamp(edited)
    author = data.get("author") if isinstance(data.get("author"), dict) else None
    if author:
        fields["display_name"] = _display_name(author, data.get("member"))
        fields["is_bot"] = 1 if author.get("bot") else 0
    reference = data.get("message_reference")
    if isinstance(reference, dict) and reference.get("message_id"):
        fields["reply_to_id"] = str(reference["message_id"])
    if store.edit_channel_message(message_id, fields):
        return
    if not author or not author.get("id") or "timestamp" not in data:
        return
    created = _normalize_timestamp(str(data.get("timestamp") or ""))
    if not created:
        return
    store.upsert_channel_message(
        {
            "message_id": message_id,
            "guild_id": str(guild_id),
            "channel_id": str(channel_id),
            "author_id": str(author["id"]),
            "display_name": fields.get("display_name"),
            "created_at": created,
            "edited_at": fields.get("edited_at"),
            "content": fields.get("content"),
            "reply_to_id": fields.get("reply_to_id"),
            "attachments_json": fields.get("attachments_json") or "[]",
            "is_bot": bool(author.get("bot")),
            "channel_name": _channel_name(data),
        }
    )


def observe_delete(payload: object, config: AppConfig, store: Store) -> None:
    guild_id = getattr(payload, "guild_id", None)
    channel_id = getattr(payload, "channel_id", None)
    message_id = str(getattr(payload, "message_id", "") or "")
    if not message_id or not _enabled(config, guild_id, channel_id):
        return
    store.delete_channel_message(message_id)


def _enabled(config: AppConfig, guild_id: object, channel_id: object) -> bool:
    if not config.cache.store_all_messages:
        return False
    if guild_id is None or channel_id is None:
        return False
    try:
        return config.allowlist.permits(int(guild_id), int(channel_id))
    except (TypeError, ValueError):
        return False


def _row_from_message(
    message: object,
    config: AppConfig,
    created: datetime,
    author_id: str,
) -> dict:
    author = getattr(message, "author", None)
    reference = getattr(message, "reference", None)
    reply_to = getattr(reference, "message_id", None) if reference is not None else None
    channel = getattr(message, "channel", None)
    return {
        "message_id": str(message.id),
        "guild_id": str(config.guild_id),
        "channel_id": str(channel.id),
        "channel_name": getattr(channel, "name", None),
        "author_id": author_id,
        "display_name": _clip(_live_display_name(author), _MAX_NAME),
        "created_at": format_precise(created),
        "edited_at": _optional_time(getattr(message, "edited_at", None)),
        "content": _clip(str(getattr(message, "content", "") or ""), _MAX_CONTENT),
        "reply_to_id": str(reply_to) if reply_to else None,
        "attachments_json": json.dumps(
            _attachments(getattr(message, "attachments", [])),
            ensure_ascii=False,
        ),
        "is_bot": bool(getattr(author, "bot", False)),
    }


def _channel_name(data: dict) -> str | None:
    name = data.get("channel_name")
    if isinstance(name, str) and name.strip():
        return _clip(name.strip(), _MAX_NAME)
    return None


def _guild_id(message: object) -> int | None:
    guild = getattr(message, "guild", None)
    return getattr(guild, "id", None)


def _channel_id(message: object) -> int | None:
    return getattr(getattr(message, "channel", None), "id", None)


def _author_id(message: object) -> str:
    author = getattr(message, "author", None)
    author_id = getattr(author, "id", None)
    return str(author_id) if author_id else ""


def _created_at(message: object) -> datetime | None:
    created = getattr(message, "created_at", None)
    if not isinstance(created, datetime):
        return None
    if created.tzinfo is None:
        return created.replace(tzinfo=UTC)
    return created


def _optional_time(value: object) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return format_precise(value)


def _live_display_name(author: object) -> str | None:
    if author is None:
        return None
    for attr in ("display_name", "global_name", "name"):
        value = getattr(author, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _display_name(author: dict, member: object) -> str | None:
    nick = member.get("nick") if isinstance(member, dict) else None
    if isinstance(nick, str) and nick.strip():
        return _clip(nick.strip(), _MAX_NAME)
    for key in ("global_name", "username"):
        value = author.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value.strip(), _MAX_NAME)
    return None


def _attachments(items: object) -> list[dict]:
    if not isinstance(items, list | tuple):
        items = list(items) if items else []
    stored: list[dict] = []
    for item in list(items)[:_MAX_ATTACHMENTS]:
        if isinstance(item, dict):
            url = item.get("url")
            filename = item.get("filename")
            content_type = item.get("content_type")
            size = item.get("size")
            attachment_id = item.get("id")
        else:
            url = getattr(item, "url", None)
            filename = getattr(item, "filename", None)
            content_type = getattr(item, "content_type", None)
            size = getattr(item, "size", None)
            attachment_id = getattr(item, "id", None)
        if not isinstance(url, str) or not url.startswith("https://"):
            continue
        stored.append(
            {
                "id": str(attachment_id) if attachment_id else "",
                "filename": str(filename or "")[:200],
                "content_type": str(content_type or "")[:100],
                "size": size if isinstance(size, int) else None,
                "url": url[:2000],
            }
        )
    return stored


def _normalize_timestamp(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return format_precise(parsed)


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    if len(value) <= limit:
        return value
    return value[:limit]

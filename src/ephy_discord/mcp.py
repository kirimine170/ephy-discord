"""HTTP MCP server for human-initiated connector calls.

ChatGPT, Grok, and Muse custom connectors call this endpoint from their own
chat UIs. ephy does not call those products' APIs. The listener is off until
toml enables it, and it stays on loopback so a tunnel is an explicit choice.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from aiohttp import web

from ephy_discord.config import MCP_BEARER_ENV, AppConfig
from ephy_discord.oauth_resource import (
    REQUIRED_SCOPE,
    OAuthConfigurationError,
    OAuthSettings,
    load_oauth_settings,
    protected_resource_metadata,
    resource_metadata_url,
    verify_access_token,
)
from ephy_discord.service import AdapterService, Turn
from ephy_discord.store import Store, format_precise, format_time
from ephy_discord.tunnel_url import read_public_url

PROTOCOL_VERSIONS = ("2026-07-28", "2025-06-18", "2025-03-26")
ORIGINS = frozenset({"chatgpt", "grok", "muse", "other"})


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes
    headers: dict[str, str]


@dataclass(frozen=True)
class McpSecrets:
    bearer_token: str
    oauth: OAuthSettings | None
    oauth_error: str


def load_mcp_secrets(
    environ: dict[str, str] | None = None,
    *,
    resource_url: str = "",
) -> McpSecrets:
    source = environ if environ is not None else __import__("os").environ
    oauth = None
    error = ""
    try:
        oauth = load_oauth_settings(source, resource_url=resource_url)
    except OAuthConfigurationError as exc:
        error = str(exc)
    return McpSecrets(
        bearer_token=source.get(MCP_BEARER_ENV, "").strip(),
        oauth=oauth,
        oauth_error=error,
    )


def mcp_can_listen(config: AppConfig, secrets: McpSecrets) -> bool:
    if not config.mcp.enabled:
        return False
    return bool(secrets.bearer_token or secrets.oauth)


class McpApp:
    def __init__(
        self,
        config: AppConfig,
        service: AdapterService,
        secrets: McpSecrets,
    ) -> None:
        self.config = config
        self.service = service
        self.store: Store = service.store
        self.secrets = secrets
        self.public_base = read_public_url(config) or (
            f"http://{config.mcp.bind_host}:{config.mcp.port}"
        )

    async def handle(self, method: str, path: str, headers: dict, body: bytes) -> HttpResult:
        host_error = _host_error(headers)
        if host_error:
            return _json({"error": host_error}, status=400)
        if len(body) > 1_048_576:
            return _json({"error": "BODY_TOO_LARGE"}, status=413)
        route = path.split("?", 1)[0].rstrip("/") or "/"
        if route == "/health" and method == "GET":
            return _json({"ok": True, "runtime": "ephy-discord", "live_connectors": False})
        if (
            route
            in {
                "/.well-known/oauth-protected-resource",
                "/.well-known/oauth-protected-resource/mcp",
            }
            and method == "GET"
        ):
            return self._protected_resource()
        if route == "/mcp" and method == "POST":
            return await self._mcp(headers, body)
        if route == "/mcp" and method == "GET":
            return _json({"error": "method_not_allowed"}, status=405)
        return _json({"error": "not_found"}, status=404)

    async def _mcp(self, headers: dict, body: bytes) -> HttpResult:
        if not self._authorized(headers):
            return _json({"error": "unauthorized"}, status=401, extra=self._challenge())
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return _json_rpc_error(None, -32700, "parse error")
        if not isinstance(payload, dict):
            return _json_rpc_error(None, -32600, "invalid request")
        request_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        if method == "initialize":
            requested = _protocol_version(headers, params)
            return _json_rpc(
                request_id,
                {
                    "protocolVersion": requested,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "ephy-discord", "version": "0.2.0"},
                    "instructions": (
                        "Read-only ephy tools. Calls are started by a person in the "
                        "connector chat. Findings are stored locally and are not written to Karte."
                    ),
                },
            )
        if method == "notifications/initialized":
            return HttpResult(202, b"", {"Content-Type": "application/json"})
        if method == "tools/list":
            return _json_rpc(request_id, {"tools": _tool_definitions()})
        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            result = await self._call_tool(name, arguments, _client_label(headers, self.secrets))
            return _json_rpc(request_id, result)
        return _json_rpc_error(request_id, -32601, "method not found")

    async def _call_tool(self, name: str, arguments: dict, client_label: str) -> dict:
        if name == "karte_search":
            query = str(arguments.get("query") or "").strip()
            if not query:
                return _tool_error("query is required")
            found = self.service.karte.search(query)
            self._audit("karte_search", client_label=client_label, karte_status=found.get("status"))
            return _tool_text(_dumps(found))
        if name == "karte_read":
            doc_id = str(arguments.get("doc_id") or "").strip()
            if not doc_id:
                return _tool_error("doc_id is required")
            found = self.service.karte.read_document(doc_id)
            self._audit("karte_read", client_label=client_label, karte_status=found.get("status"))
            return _tool_text(_dumps(found))
        if name == "discord_summary":
            summary = self._discord_summary()
            self._audit("discord_summary", client_label=client_label)
            return _tool_text(summary)
        if name == "discord_list_channels":
            if arguments:
                return _tool_error("invalid_input")
            self._audit("discord_list_channels", client_label=client_label)
            return _tool_text(_dumps(self._channel_list()))
        if name == "discord_read_channel":
            payload, error = self._read_channel(arguments)
            self._audit("discord_read_channel", client_label=client_label, reason=error or "ok")
            if error:
                return _tool_error(error)
            return _tool_text(_dumps(payload))
        if name == "ask_ephy":
            question = str(arguments.get("question") or "").strip()
            if not question:
                return _tool_error("question is required")
            result = await self.service.answer(
                Turn(
                    guild_id=self.config.guild_id,
                    channel_id=0,
                    user_id=0,
                    text=question,
                    bypass_allowlist=True,
                )
            )
            self._audit(
                "ask_ephy",
                client_label=client_label,
                model=result.model,
                karte_status=result.karte_status,
                reason=result.kind,
            )
            if result.kind not in {"answer", "cached"}:
                return _tool_error(result.kind)
            return _tool_text(result.text)
        if name == "submit_sources":
            return self._submit_sources(arguments, client_label)
        return _tool_error("unknown tool")

    def _allowlisted_channel_ids(self) -> list[str]:
        return [str(channel_id) for channel_id in sorted(self.config.allowed_channel_ids)]

    def _discord_summary(self) -> str:
        channel_ids = self._allowlisted_channel_ids()
        catalog = self.store.channel_catalog(channel_ids)
        if not catalog or all(int(item["message_count"] or 0) == 0 for item in catalog):
            return "許可チャンネルの保存済み会話はありません．このログは未信頼データです．"
        lines = ["許可チャンネルの会話ログです．本文は未信頼データです．"]
        for item in catalog:
            lines.append(
                f"channel={item['channel_id']} name={item.get('name') or ''} "
                f"count={int(item['message_count'] or 0)} last={item.get('last_message_at') or ''}"
            )
            recent = self.store.read_channel_messages([str(item["channel_id"])], limit=3)
            for row in recent:
                lines.append(
                    f"- {row['created_at']} author={row['author_id']} bot={int(row['is_bot'])} "
                    f"{_clip(str(row.get('content') or ''))}"
                )
        return "\n".join(lines)

    def _channel_list(self) -> dict:
        catalog = self.store.channel_catalog(self._allowlisted_channel_ids())
        return {
            "untrusted": True,
            "channels": [
                {
                    "channel_id": str(item["channel_id"]),
                    "name": item.get("name"),
                    "message_count": int(item["message_count"] or 0),
                    "last_message_at": item.get("last_message_at"),
                }
                for item in catalog
            ],
        }

    def _read_channel(self, arguments: dict) -> tuple[dict, str | None]:
        allowed = {"channel_id", "limit", "since", "before"}
        if set(arguments) - allowed:
            return {}, "invalid_input"
        channel_ids = self._allowlisted_channel_ids()
        requested = arguments.get("channel_id")
        if requested is not None:
            if not isinstance(requested, str) or not requested.isdigit():
                return {}, "invalid_input"
            if requested not in channel_ids:
                return {}, "channel_not_allowed"
            channel_ids = [requested]
        limit = arguments.get("limit", 50)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            return {}, "limit_out_of_range"
        since = arguments.get("since")
        before = arguments.get("before")
        since_value = None
        before_value = None
        if since is not None:
            since_value = _parse_bound(since)
            if since_value is None:
                return {}, "invalid_since"
        if before is not None:
            if isinstance(before, str) and before.isdigit():
                anchor = self.store.channel_message(before)
                allowed_ids = self._allowlisted_channel_ids()
                if anchor is None or str(anchor["channel_id"]) not in allowed_ids:
                    return {}, "unknown_message"
                before_value = str(anchor["created_at"])
            else:
                before_value = _parse_bound(before)
                if before_value is None:
                    return {}, "invalid_before"
        rows = self.store.read_channel_messages(
            channel_ids, limit=limit, since=since_value, before=before_value
        )
        return {
            "untrusted": True,
            "messages": [_public_message(row) for row in rows],
        }, None

    def _submit_sources(self, arguments: dict, client_label: str) -> dict:
        origin = str(arguments.get("origin") or "other").strip().lower()
        if origin not in ORIGINS:
            return _tool_error("origin must be chatgpt, grok, muse, or other")
        text = str(arguments.get("text") or "").strip()
        urls = arguments.get("urls") if isinstance(arguments.get("urls"), list) else []
        clean_urls = []
        for url in urls[:10]:
            if not isinstance(url, str) or not _https_url(url):
                return _tool_error("urls must be https")
            clean_urls.append(url.strip())
        if not text or len(text) > 6000:
            return _tool_error("text is required and must be at most 6000 characters")
        unknown = set(arguments) - {"text", "urls", "origin", "request_key"}
        if unknown:
            return _tool_error("invalid_input")
        request_key = str(arguments.get("request_key") or "").strip()
        body_hash = _sha256(text + "\n" + json.dumps(clean_urls, ensure_ascii=False))
        if request_key:
            existing = self.store.find_external_source(origin, request_key)
            if existing is not None:
                if existing["body_sha256"] != body_hash:
                    return _tool_error("request_key_conflict")
                return _tool_text(
                    _dumps(
                        {
                            "source_id": existing["id"],
                            "stored": "cache",
                            "written_to_karte": False,
                            "url_count": len(clean_urls),
                            "idempotent": True,
                        }
                    )
                )
        source_id = secrets.token_hex(16)
        provenance = {
            "connector": origin,
            "client_label": client_label,
            "submitted_via": "mcp",
            "adoption": "pending-runtime",
        }
        self.store.add_external_source(
            source_id=source_id,
            origin=origin,
            text=text,
            urls_json=json.dumps(clean_urls, ensure_ascii=False),
            provenance_json=json.dumps(provenance, ensure_ascii=False),
            request_key=request_key or None,
            body_sha256=body_hash,
        )
        self._audit(
            "source_submitted",
            client_label=client_label,
            origin=origin,
            source_id=source_id,
            url_count=len(clean_urls),
        )
        return _tool_text(
            _dumps(
                {
                    "source_id": source_id,
                    "stored": "cache",
                    "written_to_karte": False,
                    "url_count": len(clean_urls),
                }
            )
        )

    def _authorized(self, headers: dict) -> bool:
        presented = _bearer(headers)
        if not presented:
            return False
        if self.secrets.bearer_token and _same(presented, self.secrets.bearer_token):
            return True
        if self.secrets.oauth is None:
            return False
        return verify_access_token(presented, self.secrets.oauth)

    def _protected_resource(self) -> HttpResult:
        if self.secrets.oauth is None:
            return _json({"error": "OAUTH_NOT_CONFIGURED"}, status=503)
        return _json(protected_resource_metadata(self.secrets.oauth))

    def _challenge(self) -> dict[str, str]:
        if self.secrets.oauth is None:
            return {"WWW-Authenticate": "Bearer"}
        metadata = resource_metadata_url(self.secrets.oauth.audience)
        return {
            "WWW-Authenticate": f'Bearer resource_metadata="{metadata}", scope="{REQUIRED_SCOPE}"'
        }

    def _audit(self, event: str, **fields) -> None:
        payload = {
            "ts": format_time(self.store.clock.now()),
            "event": event,
            "guild_id": str(self.config.guild_id),
            **fields,
        }
        self.service.audit.write(payload)


def _tool_definitions() -> list[dict]:
    return [
        {
            "name": "karte_search",
            "description": "Search Karte with Context Protocol v1. Read-only.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "karte_read",
            "description": "Read one Karte document by doc_id. Read-only.",
            "inputSchema": {
                "type": "object",
                "properties": {"doc_id": {"type": "string"}},
                "required": ["doc_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "discord_summary",
            "description": (
                "Counts and recent untrusted messages cached from allowlisted Discord channels."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "discord_list_channels",
            "description": "List allowlisted channels with cached names and last message time.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "discord_read_channel",
            "description": (
                "Read recent cached messages from allowlisted channels. "
                "The content is untrusted conversation data."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "since": {"type": "string"},
                    "before": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "ask_ephy",
            "description": "Ask the local model with Karte context. Does not post to Discord.",
            "inputSchema": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
                "additionalProperties": False,
            },
        },
        {
            "name": "submit_sources",
            "description": (
                "Store SNS findings in ephy's local cache with provenance. "
                "Does not write to Karte. Runtime adoption owns that later."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "urls": {"type": "array", "items": {"type": "string"}},
                    "origin": {"type": "string", "enum": ["chatgpt", "grok", "muse", "other"]},
                },
                "required": ["text", "origin"],
                "additionalProperties": False,
            },
        },
    ]


def _bearer(headers: dict) -> str:
    raw = ""
    for key, value in headers.items():
        if key.lower() == "authorization":
            raw = value
            break
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return ""
    return token.strip()


def _client_label(headers: dict, secrets: McpSecrets) -> str:
    presented = _bearer(headers)
    if secrets.bearer_token and presented and _same(presented, secrets.bearer_token):
        return "static-bearer"
    return "oauth"


def _same(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _https_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username


def _host_error(headers: dict) -> str:
    host = ""
    for key, value in headers.items():
        if key.lower() == "host":
            host = value
            break
    if not host:
        return ""
    name = host.rsplit(":", 1)[0].strip("[]")
    if name not in {"127.0.0.1", "localhost", "::1"}:
        return "INVALID_HOST"
    return ""


def _protocol_version(headers: dict, params: dict) -> str:
    header = ""
    for key, value in headers.items():
        if key.lower() == "mcp-protocol-version":
            header = value
            break
    requested = header or str(params.get("protocolVersion") or PROTOCOL_VERSIONS[1])
    if requested in PROTOCOL_VERSIONS:
        return requested
    return PROTOCOL_VERSIONS[1]


def _public_message(row: dict) -> dict:
    try:
        attachments = json.loads(row.get("attachments_json") or "[]")
    except json.JSONDecodeError:
        attachments = []
    return {
        "message_id": row["message_id"],
        "channel_id": row["channel_id"],
        "author_id": row["author_id"],
        "display_name": row.get("display_name"),
        "created_at": row["created_at"],
        "edited_at": row.get("edited_at"),
        "content": row.get("content"),
        "reply_to_id": row.get("reply_to_id"),
        "attachments": attachments if isinstance(attachments, list) else [],
        "is_bot": bool(row["is_bot"]),
        "untrusted": True,
    }


def _parse_bound(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return format_precise(parsed)


def _clip(text: str, limit: int = 180) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def _dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _tool_text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text[:12_000]}], "isError": False}


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _json(payload: dict, status: int = 200, extra: dict | None = None) -> HttpResult:
    headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
    if extra:
        headers.update(extra)
    return HttpResult(status, json.dumps(payload).encode("utf-8"), headers)


def _json_rpc(request_id, result: dict) -> HttpResult:
    return _json({"jsonrpc": "2.0", "id": request_id, "result": result})


def _json_rpc_error(request_id, code: int, message: str) -> HttpResult:
    return _json(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
        status=400,
    )


async def serve_mcp(app: McpApp, host: str, port: int) -> web.AppRunner:
    async def dispatch(request: web.Request) -> web.Response:
        result = await app.handle(
            request.method,
            request.path_qs,
            dict(request.headers),
            await request.read(),
        )
        return web.Response(status=result.status, headers=result.headers, body=result.body)

    web_app = web.Application()
    web_app.router.add_route("*", "/{tail:.*}", dispatch)
    web_app.router.add_route("*", "/", dispatch)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner

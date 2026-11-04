"""HTTP MCP server for human-initiated connector calls.

ChatGPT, Grok, and Muse custom connectors call this endpoint from their own
chat UIs. ephy does not call those products' APIs. The listener is off until
toml enables it, and it stays on loopback so a tunnel is an explicit choice.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlparse

from aiohttp import web

from ephy_discord.config import (
    MCP_BEARER_ENV,
    MCP_OAUTH_ENV,
    AppConfig,
)
from ephy_discord.service import AdapterService, Turn
from ephy_discord.store import Store, format_time

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
ORIGINS = frozenset({"chatgpt", "grok", "muse", "other"})


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes
    headers: dict[str, str]


@dataclass(frozen=True)
class McpSecrets:
    bearer_token: str
    oauth_secret: str


def load_mcp_secrets(environ: dict[str, str] | None = None) -> McpSecrets:
    source = environ if environ is not None else __import__("os").environ
    return McpSecrets(
        bearer_token=source.get(MCP_BEARER_ENV, "").strip(),
        oauth_secret=source.get(MCP_OAUTH_ENV, "").strip(),
    )


def mcp_can_listen(config: AppConfig, secrets: McpSecrets) -> bool:
    if not config.mcp.enabled:
        return False
    return bool(secrets.bearer_token or secrets.oauth_secret)


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
        self.public_base = config.mcp.public_base_url or (
            f"http://{config.mcp.bind_host}:{config.mcp.port}"
        )

    async def handle(self, method: str, path: str, headers: dict, body: bytes) -> HttpResult:
        route = path.split("?", 1)[0].rstrip("/") or "/"
        if route == "/.well-known/oauth-protected-resource" and method == "GET":
            return _json(self._protected_resource())
        if route == "/.well-known/oauth-authorization-server" and method == "GET":
            return _json(self._authorization_server())
        if route == "/oauth/register" and method == "POST":
            return self._register(body)
        if route == "/oauth/authorize" and method in {"GET", "POST"}:
            return self._authorize(method, path, body)
        if route == "/oauth/token" and method == "POST":
            return self._token(body, headers)
        if route == "/mcp" and method == "POST":
            return await self._mcp(headers, body)
        if route == "/mcp" and method == "GET":
            return _json({"error": "method_not_allowed"}, status=405)
        return _json({"error": "not_found"}, status=404)

    async def _mcp(self, headers: dict, body: bytes) -> HttpResult:
        if not self._authorized(headers):
            return _json(
                {"error": "unauthorized"},
                status=401,
                extra={
                    "WWW-Authenticate": (
                        'Bearer resource_metadata="'
                        f'{self.public_base}/.well-known/oauth-protected-resource"'
                    )
                },
            )
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
            requested = str(params.get("protocolVersion") or PROTOCOL_VERSIONS[0])
            version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _json_rpc(
                request_id,
                {
                    "protocolVersion": version,
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

    def _discord_summary(self) -> str:
        channel_ids = [str(channel_id) for channel_id in sorted(self.config.allowed_channel_ids)]
        rows = self.store.recent_messages(channel_ids, limit=20)
        if not rows:
            return "許可チャンネルの保存済み会話はありません．"
        lines = [f"許可チャンネルの保存済み会話 {len(rows)} 件です．"]
        for row in rows:
            question = _clip(str(row.get("query_text") or ""))
            answer = _clip(str(row.get("answer_text") or ""))
            lines.append(
                f"- {row['created_at']} channel={row['channel_id']} user={row['user_id']}\n"
                f"  Q: {question}\n  A: {answer}"
            )
        return "\n".join(lines)

    def _submit_sources(self, arguments: dict, client_label: str) -> dict:
        origin = str(arguments.get("origin") or "other").strip().lower()
        if origin not in ORIGINS:
            return _tool_error("origin must be chatgpt, grok, muse, or other")
        text = str(arguments.get("text") or "").strip()
        urls = arguments.get("urls") if isinstance(arguments.get("urls"), list) else []
        clean_urls = []
        for url in urls[:10]:
            if not isinstance(url, str) or not _public_http_url(url):
                return _tool_error("urls must be http or https")
            clean_urls.append(url.strip())
        if not text or len(text) > 8000:
            return _tool_error("text is required and must be at most 8000 characters")
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

    def _register(self, body: bytes) -> HttpResult:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return _json({"error": "invalid_client_metadata"}, status=400)
        redirects = payload.get("redirect_uris") if isinstance(payload, dict) else None
        if not isinstance(redirects, list) or not redirects:
            return _json({"error": "invalid_redirect_uri"}, status=400)
        clean = []
        for uri in redirects:
            if not isinstance(uri, str) or not _allowed_redirect(uri):
                return _json({"error": "invalid_redirect_uri"}, status=400)
            clean.append(uri)
        client_id = "ephy-" + secrets.token_hex(8)
        client_secret = secrets.token_urlsafe(32)
        self.store.save_oauth_client(
            client_id,
            _sha256(client_secret),
            json.dumps(clean, ensure_ascii=False),
        )
        return _json(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uris": clean,
                "token_endpoint_auth_method": "client_secret_post",
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
            },
            status=201,
        )

    def _authorize(self, method: str, path: str, body: bytes) -> HttpResult:
        if method == "GET":
            params = parse_qs(urlparse(path).query)
            flat = {key: values[-1] for key, values in params.items()}
        else:
            flat = {key: values[-1] for key, values in parse_qs(body.decode("utf-8")).items()}
        error = self._authorize_error(flat)
        if error:
            return _json({"error": error}, status=400)
        if method == "GET" or not _same(flat.get("owner_secret", ""), self.secrets.oauth_secret):
            return HttpResult(
                200,
                _authorize_page(flat).encode("utf-8"),
                {"Content-Type": "text/html; charset=utf-8"},
            )
        code = secrets.token_urlsafe(32)
        expires = format_time(self.store.clock.now() + timedelta(seconds=60))
        self.store.save_oauth_code(
            {
                "code_hash": _sha256(code),
                "client_id": flat["client_id"],
                "redirect_uri": flat["redirect_uri"],
                "challenge": flat["code_challenge"],
                "expires_at": expires,
            }
        )
        query = urlencode({"code": code, "state": flat.get("state", "")})
        return HttpResult(302, b"", {"Location": f"{flat['redirect_uri']}?{query}"})

    def _authorize_error(self, flat: dict) -> str:
        if flat.get("response_type") != "code":
            return "unsupported_response_type"
        if flat.get("code_challenge_method") != "S256" or not flat.get("code_challenge"):
            return "invalid_request"
        client = self.store.get_oauth_client(flat.get("client_id", ""))
        if client is None:
            return "invalid_client"
        redirects = json.loads(client["redirect_uris_json"])
        if flat.get("redirect_uri") not in redirects:
            return "invalid_redirect_uri"
        if not self.secrets.oauth_secret:
            return "oauth_disabled"
        return ""

    def _token(self, body: bytes, headers: dict) -> HttpResult:
        form = {key: values[-1] for key, values in parse_qs(body.decode("utf-8")).items()}
        if form.get("grant_type") != "authorization_code":
            return _json({"error": "unsupported_grant_type"}, status=400)
        client = self.store.get_oauth_client(form.get("client_id", ""))
        presented = form.get("client_secret", "")
        if client is None or not _same(_sha256(presented), client["secret_hash"]):
            return _json({"error": "invalid_client"}, status=401)
        now_iso = format_time(self.store.clock.now())
        code_hash = _sha256(form.get("code", ""))
        code = self.store.peek_oauth_code(code_hash, now_iso)
        if code is None or code["client_id"] != client["client_id"]:
            return _json({"error": "invalid_grant"}, status=400)
        if code["redirect_uri"] != form.get("redirect_uri"):
            return _json({"error": "invalid_grant"}, status=400)
        verifier = form.get("code_verifier", "")
        if not _same(_pkce(verifier), code["challenge"]):
            return _json({"error": "invalid_grant"}, status=400)
        if self.store.take_oauth_code(code_hash, now_iso) is None:
            return _json({"error": "invalid_grant"}, status=400)
        token = secrets.token_urlsafe(32)
        expires = self.store.clock.now() + timedelta(seconds=self.config.mcp.token_ttl_seconds)
        self.store.save_oauth_token(
            token_hash=_sha256(token),
            client_id=client["client_id"],
            expires_at=format_time(expires),
        )
        del headers
        return _json(
            {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": self.config.mcp.token_ttl_seconds,
            }
        )

    def _authorized(self, headers: dict) -> bool:
        presented = _bearer(headers)
        if not presented:
            return False
        if self.secrets.bearer_token and _same(presented, self.secrets.bearer_token):
            return True
        return self.store.oauth_token_valid(_sha256(presented), format_time(self.store.clock.now()))

    def _protected_resource(self) -> dict:
        return {
            "resource": self.public_base,
            "authorization_servers": [self.public_base],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["ephy:read", "ephy:submit"],
        }

    def _authorization_server(self) -> dict:
        return {
            "issuer": self.public_base,
            "authorization_endpoint": f"{self.public_base}/oauth/authorize",
            "token_endpoint": f"{self.public_base}/oauth/token",
            "registration_endpoint": f"{self.public_base}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
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
            "description": "Summarize cached messages from allowlisted Discord channels.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
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


def _authorize_page(params: dict) -> str:
    hidden = "".join(
        f'<input type="hidden" name="{_escape(key)}" value="{_escape(params.get(key, ""))}">'
        for key in (
            "response_type",
            "client_id",
            "redirect_uri",
            "code_challenge",
            "code_challenge_method",
            "state",
        )
    )
    return (
        '<!DOCTYPE html><html lang="ja"><meta charset="utf-8">'
        "<title>ephy connector</title><body>"
        "<p>コネクタに ephy の読み取りを許可します．合言葉を入力してください．</p>"
        f'<form method="post" action="/oauth/authorize">{hidden}'
        '<label>合言葉 <input type="password" name="owner_secret" autocomplete="off"></label>'
        '<button type="submit">許可する</button></form></body></html>'
    )


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


def _pkce(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _same(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _allowed_redirect(uri: str) -> bool:
    parsed = urlparse(uri)
    if parsed.username or parsed.query or parsed.fragment:
        return False
    if parsed.scheme == "https" and parsed.hostname:
        return True
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def _public_http_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


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


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
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

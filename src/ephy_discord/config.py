"""TOML configuration. Unknown keys are rejected so typos do not silently widen access."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SENSITIVITY = frozenset({"public", "internal", "confidential", "restricted"})
_GATEWAY_MODES = frozenset({"auto", "fast", "work", "code", "rag"})
_KARTE_MODES = frozenset({"auto", "filesystem", "stub"})
_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

TOKEN_ENV = "DISCORD_BOT_TOKEN"


@dataclass(frozen=True)
class LlmConfig:
    base_url: str
    model: str
    timeout_seconds: float
    temperature: float
    max_tokens: int
    via_gateway: bool
    gateway_mode: str


@dataclass(frozen=True)
class KarteConfig:
    mode: str
    data_dir: str
    timeout_seconds: float
    poll_interval_seconds: float
    projects: tuple[str, ...]
    tags: tuple[str, ...]
    sensitivity_ceiling: str
    top_k: int
    max_documents: int
    actor_id: str


@dataclass(frozen=True)
class CacheConfig:
    path: str
    dedupe_seconds: int
    retention_days: int


@dataclass(frozen=True)
class RateLimitConfig:
    user_per_minute: int
    channel_per_minute: int
    window_seconds: int


@dataclass(frozen=True)
class AuditConfig:
    path: str


@dataclass(frozen=True)
class AppConfig:
    guild_id: int
    allowed_channel_ids: frozenset[int]
    admin_user_ids: frozenset[int]
    llm: LlmConfig
    karte: KarteConfig
    cache: CacheConfig
    rate_limit: RateLimitConfig
    audit: AuditConfig

    @property
    def allowlist(self):
        from ephy_discord.allowlist import Allowlist

        return Allowlist(self.guild_id, self.allowed_channel_ids)


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("config root must be a table")
    _reject_unknown(raw, {"discord", "llm", "karte", "cache", "rate_limit", "audit"}, "root")
    return AppConfig(
        **_discord_section(raw.get("discord", {})),
        llm=_llm_section(raw.get("llm", {})),
        karte=_karte_section(raw.get("karte", {})),
        cache=_cache_section(raw.get("cache", {})),
        rate_limit=_rate_section(raw.get("rate_limit", {})),
        audit=_audit_section(raw.get("audit", {})),
    )


def resolved_karte_dir(config: AppConfig) -> str | None:
    configured = config.karte.data_dir.strip()
    if configured:
        return configured
    from_env = os.environ.get("KARTE_DATA_DIR", "").strip()
    return from_env or None


def read_bot_token() -> str:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise RuntimeError(f"{TOKEN_ENV} is not set")
    return token


def _discord_section(raw: object) -> dict:
    data = _table(raw, "discord")
    _reject_unknown(data, {"guild_id", "allowed_channel_ids", "admin_user_ids"}, "discord")
    return {
        "guild_id": _snowflake(data.get("guild_id"), "discord.guild_id"),
        "allowed_channel_ids": frozenset(
            _snowflake_list(data.get("allowed_channel_ids", []), "discord.allowed_channel_ids")
        ),
        "admin_user_ids": frozenset(
            _snowflake_list(data.get("admin_user_ids", []), "discord.admin_user_ids")
        ),
    }


def _llm_section(raw: object) -> LlmConfig:
    data = _table(raw, "llm")
    _reject_unknown(
        data,
        {
            "base_url",
            "model",
            "timeout_seconds",
            "temperature",
            "max_tokens",
            "via_gateway",
            "gateway_mode",
        },
        "llm",
    )
    mode = str(data.get("gateway_mode", "fast"))
    if mode not in _GATEWAY_MODES:
        raise ValueError("llm.gateway_mode is not a known ephy-runtime route")
    return LlmConfig(
        base_url=_loopback_base_url(str(data.get("base_url", "http://127.0.0.1:8000/v1"))),
        model=_bounded_text(data.get("model", "auto"), "llm.model", 128),
        timeout_seconds=_number(data.get("timeout_seconds", 120), "llm.timeout_seconds", 1, 600),
        temperature=_number(data.get("temperature", 0.7), "llm.temperature", 0, 2),
        max_tokens=int(_number(data.get("max_tokens", 800), "llm.max_tokens", 1, 8192)),
        via_gateway=_bool(data.get("via_gateway", True), "llm.via_gateway"),
        gateway_mode=mode,
    )


def _karte_section(raw: object) -> KarteConfig:
    data = _table(raw, "karte")
    _reject_unknown(
        data,
        {
            "mode",
            "data_dir",
            "timeout_seconds",
            "poll_interval_seconds",
            "projects",
            "tags",
            "sensitivity_ceiling",
            "top_k",
            "max_documents",
            "actor_id",
        },
        "karte",
    )
    mode = str(data.get("mode", "auto"))
    if mode not in _KARTE_MODES:
        raise ValueError("karte.mode must be auto, filesystem, or stub")
    ceiling = str(data.get("sensitivity_ceiling", "internal"))
    if ceiling not in _SENSITIVITY:
        raise ValueError("karte.sensitivity_ceiling is invalid")
    projects = tuple(_string_list(data.get("projects", []), "karte.projects"))
    for project in projects:
        if project != "*" and _PROJECT_RE.fullmatch(project) is None:
            raise ValueError("karte.projects contains an invalid slug")
    tags = tuple(_string_list(data.get("tags", []), "karte.tags"))
    for tag in tags:
        if not tag.strip() or len(tag) > 128:
            raise ValueError("karte.tags contains an invalid tag")
    actor_id = str(data.get("actor_id", "ephy-discord"))
    if _ACTOR_RE.fullmatch(actor_id) is None:
        raise ValueError("karte.actor_id is invalid")
    return KarteConfig(
        mode=mode,
        data_dir=str(data.get("data_dir", "")),
        timeout_seconds=_number(data.get("timeout_seconds", 3), "karte.timeout_seconds", 0.05, 30),
        poll_interval_seconds=_number(
            data.get("poll_interval_seconds", 0.05), "karte.poll_interval_seconds", 0.01, 1
        ),
        projects=projects,
        tags=tags,
        sensitivity_ceiling=ceiling,
        top_k=int(_number(data.get("top_k", 5), "karte.top_k", 1, 20)),
        max_documents=int(_number(data.get("max_documents", 3), "karte.max_documents", 1, 3)),
        actor_id=actor_id,
    )


def _cache_section(raw: object) -> CacheConfig:
    data = _table(raw, "cache")
    _reject_unknown(data, {"path", "dedupe_seconds", "retention_days"}, "cache")
    return CacheConfig(
        path=str(data.get("path", "data/ephy-discord.sqlite")),
        dedupe_seconds=int(
            _number(data.get("dedupe_seconds", 600), "cache.dedupe_seconds", 1, 86_400)
        ),
        retention_days=int(_number(data.get("retention_days", 7), "cache.retention_days", 1, 3650)),
    )


def _rate_section(raw: object) -> RateLimitConfig:
    data = _table(raw, "rate_limit")
    _reject_unknown(data, {"user_per_minute", "channel_per_minute", "window_seconds"}, "rate_limit")
    return RateLimitConfig(
        user_per_minute=int(
            _number(data.get("user_per_minute", 3), "rate_limit.user_per_minute", 1, 1000)
        ),
        channel_per_minute=int(
            _number(data.get("channel_per_minute", 10), "rate_limit.channel_per_minute", 1, 10_000)
        ),
        window_seconds=int(
            _number(data.get("window_seconds", 60), "rate_limit.window_seconds", 1, 3600)
        ),
    )


def _audit_section(raw: object) -> AuditConfig:
    data = _table(raw, "audit")
    _reject_unknown(data, {"path"}, "audit")
    return AuditConfig(path=str(data.get("path", "data/audit.jsonl")))


def _loopback_base_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK_HOSTS:
        raise ValueError("llm.base_url must be http on 127.0.0.1, localhost, or ::1")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("llm.base_url must not include credentials, query, or fragment")
    if parsed.port is None:
        raise ValueError("llm.base_url must include an explicit port")
    path = parsed.path.rstrip("/") or "/v1"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _table(raw: object, name: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{name} must be a table")
    return raw


def _reject_unknown(data: dict, allowed: set[str], name: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"{name} has unknown keys: {', '.join(unknown)}")


def _snowflake(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValueError(f"{name} must be an integer id")
    if isinstance(value, str):
        if not value.isdigit():
            raise ValueError(f"{name} must be an integer id")
        value = int(value)
    if value < 1:
        raise ValueError(f"{name} must be a positive id")
    return value


def _snowflake_list(value: object, name: str) -> list[int]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return [_snowflake(item, name) for item in value]


def _string_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be an array of strings")
    return value


def _bounded_text(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be a short non-empty string")
    return value.strip()


def _number(value: object, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if number < low or number > high:
        raise ValueError(f"{name} is outside {low}..{high}")
    return number


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value

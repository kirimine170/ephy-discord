from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ephy_discord.config import (
    AppConfig,
    AuditConfig,
    CacheConfig,
    KarteConfig,
    LlmConfig,
    RateLimitConfig,
)
from ephy_discord.karte import ContextPack
from ephy_discord.llm import LlmResult
from ephy_discord.service import AdapterService
from ephy_discord.store import Clock, Store


class ManualClock(Clock):
    def __init__(self, moment: datetime | None = None) -> None:
        self.moment = moment or datetime(2026, 11, 4, tzinfo=UTC)

    def now(self) -> datetime:
        return self.moment

    def advance(self, *, seconds: int = 0, days: int = 0) -> None:
        self.moment += timedelta(seconds=seconds, days=days)


class FakeLlm:
    def __init__(self, text: str = "答えです．", model: str = "mock-model") -> None:
        self.text = text
        self.model = model
        self.calls: list[list[dict]] = []
        self.error: Exception | None = None

    async def complete(self, messages: list[dict]) -> LlmResult:
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return LlmResult(self.text, self.model)

    async def aclose(self) -> None:
        return None


class FakeKarte:
    def __init__(self, pack: ContextPack | None = None) -> None:
        self.pack = pack or ContextPack(status="ok", sources=())
        self.queries: list[str] = []

    def search_and_read(self, query: str) -> ContextPack:
        self.queries.append(query)
        return self.pack


class FakeCtx:
    def __init__(
        self,
        *,
        guild_id: int = 1,
        channel_id: int = 10,
        user_id: int = 100,
        is_admin: bool = False,
        message_id: str | None = None,
    ) -> None:
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.user_id = user_id
        self.is_admin = is_admin
        self.message_id = message_id
        self.replies: list[dict] = []
        self.shutdowns = 0

    async def reply(self, content: str, *, ephemeral: bool = False) -> None:
        self.replies.append({"content": content, "ephemeral": ephemeral})

    async def shutdown(self) -> None:
        self.shutdowns += 1


def make_config(
    tmp_path,
    *,
    guild_id: int = 1,
    channels: tuple[int, ...] = (10,),
    admins: tuple[int, ...] = (),
    user_per_minute: int = 3,
    channel_per_minute: int = 10,
    dedupe_seconds: int = 600,
    retention_days: int = 7,
    window_seconds: int = 60,
    karte_mode: str = "stub",
) -> AppConfig:
    return AppConfig(
        guild_id=guild_id,
        allowed_channel_ids=frozenset(channels),
        admin_user_ids=frozenset(admins),
        llm=LlmConfig(
            base_url="http://127.0.0.1:9/v1",
            model="auto",
            timeout_seconds=5,
            temperature=0.7,
            max_tokens=200,
            via_gateway=False,
            gateway_mode="fast",
        ),
        karte=KarteConfig(
            mode=karte_mode,
            data_dir="",
            timeout_seconds=1,
            poll_interval_seconds=0.01,
            projects=(),
            tags=(),
            sensitivity_ceiling="internal",
            top_k=5,
            max_documents=3,
            actor_id="ephy-discord",
        ),
        cache=CacheConfig(
            path=str(tmp_path / "cache.sqlite"),
            dedupe_seconds=dedupe_seconds,
            retention_days=retention_days,
        ),
        rate_limit=RateLimitConfig(
            user_per_minute=user_per_minute,
            channel_per_minute=channel_per_minute,
            window_seconds=window_seconds,
        ),
        audit=AuditConfig(path=str(tmp_path / "audit.jsonl")),
    )


def make_service(tmp_path, clock: ManualClock | None = None, **config_kwargs):
    config = make_config(tmp_path, **config_kwargs)
    active_clock = clock or ManualClock()
    store = Store(config.cache.path, cache=config.cache, rate=config.rate_limit, clock=active_clock)
    from ephy_discord.audit import AuditLog

    llm = FakeLlm()
    karte = FakeKarte()
    service = AdapterService(config, store, AuditLog(config.audit.path), karte, llm)
    return service, llm, karte

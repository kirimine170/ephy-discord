"""Answer pipeline: allowlist, rate limit, cache, Karte, local LLM, audit."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import timedelta

from ephy_discord.audit import AuditLog
from ephy_discord.config import AppConfig
from ephy_discord.karte import (
    FilesystemKarteClient,
    SourceRef,
    StubKarteClient,
    build_karte_client,
)
from ephy_discord.llm import LlmClient, LlmError
from ephy_discord.prompt import build_messages
from ephy_discord.store import Store, format_time, query_hash

MAX_QUESTION_CHARS = 4000

KARTE_UNAVAILABLE_NOTE = (
    "（Karteの参照はいま使えなかったので，保存された個人コンテキストは使っていません．）"
)


@dataclass(frozen=True)
class Turn:
    guild_id: int
    channel_id: int
    user_id: int
    text: str
    message_id: str | None = None
    bypass_allowlist: bool = False


@dataclass
class AnswerResult:
    kind: str
    text: str = ""
    model: str | None = None
    sources: tuple[SourceRef, ...] = ()
    latency_ms: int | None = None
    cache_hit: bool = False
    karte_status: str | None = None


class AdapterService:
    def __init__(
        self,
        config: AppConfig,
        store: Store,
        audit: AuditLog,
        karte: StubKarteClient | FilesystemKarteClient,
        llm: LlmClient,
    ) -> None:
        self.config = config
        self.store = store
        self.audit = audit
        self.karte = karte
        self.llm = llm
        self.stopped = False

    async def aclose(self) -> None:
        await self.llm.aclose()
        self.store.close()

    def stop(self) -> None:
        self.stopped = True

    def forget(self, user_id: int) -> int:
        removed = self.store.forget_user(str(user_id))
        self.audit.forget_user(str(user_id))
        self._audit(
            event="forget",
            user_id=str(user_id),
            channel_id="",
            guild_id=str(self.config.guild_id),
        )
        return removed

    def purge(self, user_id: int) -> int:
        removed = self.store.purge()
        self.audit.clear()
        self._audit(
            event="purge",
            user_id=str(user_id),
            channel_id="",
            guild_id=str(self.config.guild_id),
            reason=f"removed={removed}",
        )
        return removed

    async def answer(self, turn: Turn) -> AnswerResult:
        self._retain()
        if self.stopped:
            return AnswerResult(kind="stopped", text="いま停止中です．")
        if not turn.bypass_allowlist and not self.config.allowlist.permits(
            turn.guild_id, turn.channel_id
        ):
            return AnswerResult(kind="denied")
        question = " ".join(turn.text.split())
        if not question:
            return AnswerResult(kind="empty")
        if len(question) > MAX_QUESTION_CHARS:
            return AnswerResult(kind="too_long")

        user_id = str(turn.user_id)
        channel_id = str(turn.channel_id)
        guild_id = str(turn.guild_id)
        decision = self.store.consume_rate(user_id, channel_id)
        if not decision.allowed:
            self._audit(
                event="rate_limited",
                user_id=user_id,
                channel_id=channel_id,
                guild_id=guild_id,
                reason=decision.reason,
            )
            return AnswerResult(kind="rate_limited")

        digest = query_hash(question, self.config.llm.model)
        cached = self.store.find_cached(digest)
        if cached is not None:
            sources = _sources_from_json(cached.sources_json)
            self.store.remember(
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                discord_message_id=turn.message_id,
                query_text=question,
                answer_text=cached.answer_text,
                digest=digest,
                model=cached.model,
                sources_json=cached.sources_json,
                latency_ms=0,
                cache_hit=True,
            )
            self._audit(
                event="answer",
                user_id=user_id,
                channel_id=channel_id,
                guild_id=guild_id,
                model=cached.model,
                latency_ms=0,
                sources=[source.audit_dict() for source in sources],
                cache_hit=True,
                karte_status="cache",
            )
            return AnswerResult(
                kind="cached",
                text=cached.answer_text,
                model=cached.model,
                sources=tuple(sources),
                latency_ms=0,
                cache_hit=True,
                karte_status="cache",
            )

        started = time.perf_counter()
        pack = await asyncio.to_thread(self.karte.search_and_read, question)
        if self.stopped:
            return AnswerResult(kind="stopped", text="いま停止中です．")
        try:
            result = await self.llm.complete(build_messages(question, pack))
        except LlmError as exc:
            self._audit(
                event="llm_error",
                user_id=user_id,
                channel_id=channel_id,
                guild_id=guild_id,
                model=self.config.llm.model,
                latency_ms=_elapsed_ms(started),
                karte_status=pack.status,
                reason=exc.code,
                sources=[source.audit_dict() for source in pack.sources],
            )
            return AnswerResult(kind="error", karte_status=pack.status)
        if self.stopped:
            return AnswerResult(kind="stopped", text="いま停止中です．")

        text = result.text
        if pack.status != "ok":
            text = f"{text}\n\n{KARTE_UNAVAILABLE_NOTE}"
        latency_ms = _elapsed_ms(started)
        sources_json = json.dumps(
            [source.audit_dict() for source in pack.sources],
            ensure_ascii=False,
        )
        self.store.remember(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            discord_message_id=turn.message_id,
            query_text=question,
            answer_text=text,
            digest=digest,
            model=result.model,
            sources_json=sources_json,
            latency_ms=latency_ms,
            cache_hit=False,
        )
        self._audit(
            event="answer",
            user_id=user_id,
            channel_id=channel_id,
            guild_id=guild_id,
            model=result.model,
            latency_ms=latency_ms,
            sources=[source.audit_dict() for source in pack.sources],
            cache_hit=False,
            karte_status=pack.status,
        )
        return AnswerResult(
            kind="answer",
            text=text,
            model=result.model,
            sources=pack.sources,
            latency_ms=latency_ms,
            karte_status=pack.status,
        )

    def _retain(self) -> None:
        self.store.enforce_retention()
        cutoff = self.store.clock.now() - timedelta(days=self.config.cache.retention_days)
        self.audit.enforce_retention(format_time(cutoff))

    def _audit(self, **event) -> None:
        event.setdefault("ts", format_time(self.store.clock.now()))
        self.audit.write(event)


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def build_service(config: AppConfig, *, clock=None) -> AdapterService:
    store = Store(
        config.cache.path,
        cache=config.cache,
        rate=config.rate_limit,
        clock=clock,
    )
    return AdapterService(
        config,
        store,
        AuditLog(config.audit.path),
        build_karte_client(config),
        LlmClient(config.llm),
    )


def _sources_from_json(payload: str) -> list[SourceRef]:
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError:
        return []
    sources: list[SourceRef] = []
    if not isinstance(raw, list):
        return sources
    for item in raw:
        if not isinstance(item, dict):
            continue
        doc_id = str(item.get("doc_id") or "")
        title = str(item.get("title") or "")
        if not doc_id or not title:
            continue
        sources.append(
            SourceRef(
                doc_id=doc_id,
                title=title,
                relative_path=str(item.get("relative_path") or ""),
                excerpt="",
                sha256="",
            )
        )
    return sources

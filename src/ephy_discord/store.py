"""SQLite cache, rate limits, and retention. Message text is deleted when it expires."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ephy_discord.config import CacheConfig, RateLimitConfig


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def format_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def query_hash(question: str, model: str) -> str:
    normalized = " ".join(question.casefold().split())
    payload = f"{model}\n{normalized}".encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class CachedAnswer:
    answer_text: str
    model: str
    sources_json: str
    created_at: str


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    reason: str


class Clock:
    def now(self) -> datetime:
        return utc_now()


class Store:
    def __init__(
        self,
        path: str | Path,
        *,
        cache: CacheConfig,
        rate: RateLimitConfig,
        clock: Clock | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cache = cache
        self.rate = rate
        self.clock = clock or Clock()
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def enforce_retention(self, *, vacuum: bool = True) -> int:
        cutoff = format_time(self.clock.now() - timedelta(days=self.cache.retention_days))
        rate_cutoff = format_time(self.clock.now() - timedelta(seconds=self.rate.window_seconds))
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET query_text = NULL, answer_text = NULL WHERE created_at <= ?",
                (cutoff,),
            )
            deleted = self._conn.execute(
                "DELETE FROM messages WHERE created_at <= ?",
                (cutoff,),
            ).rowcount
            self._conn.execute("DELETE FROM rate_events WHERE created_at <= ?", (rate_cutoff,))
            self._conn.commit()
            if vacuum and deleted:
                self._conn.execute("VACUUM")
            return int(deleted)

    def consume_rate(self, user_id: str, channel_id: str) -> RateDecision:
        now = format_time(self.clock.now())
        cutoff = format_time(self.clock.now() - timedelta(seconds=self.rate.window_seconds))
        with self._lock:
            user_count = self._count(
                "SELECT COUNT(*) FROM rate_events WHERE user_id = ? AND created_at > ?",
                (user_id, cutoff),
            )
            if user_count >= self.rate.user_per_minute:
                return RateDecision(False, "user")
            channel_count = self._count(
                "SELECT COUNT(*) FROM rate_events WHERE channel_id = ? AND created_at > ?",
                (channel_id, cutoff),
            )
            if channel_count >= self.rate.channel_per_minute:
                return RateDecision(False, "channel")
            self._conn.execute(
                "INSERT INTO rate_events (created_at, user_id, channel_id) VALUES (?, ?, ?)",
                (now, user_id, channel_id),
            )
            self._conn.commit()
            return RateDecision(True, "")

    def find_cached(self, digest: str) -> CachedAnswer | None:
        cutoff = format_time(self.clock.now() - timedelta(seconds=self.cache.dedupe_seconds))
        with self._lock:
            row = self._conn.execute(
                """
                SELECT answer_text, model, sources_json, created_at
                FROM messages
                WHERE query_hash = ?
                  AND created_at >= ?
                  AND answer_text IS NOT NULL
                  AND cache_hit = 0
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (digest, cutoff),
            ).fetchone()
        if row is None:
            return None
        return CachedAnswer(
            answer_text=str(row["answer_text"]),
            model=str(row["model"] or ""),
            sources_json=str(row["sources_json"] or "[]"),
            created_at=str(row["created_at"]),
        )

    def remember(
        self,
        *,
        guild_id: str,
        channel_id: str,
        user_id: str,
        discord_message_id: str | None,
        query_text: str,
        answer_text: str,
        digest: str,
        model: str,
        sources_json: str,
        latency_ms: int | None,
        cache_hit: bool,
    ) -> None:
        now = format_time(self.clock.now())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO messages (
                    created_at, guild_id, channel_id, user_id, discord_message_id,
                    query_text, answer_text, query_hash, model, sources_json, latency_ms, cache_hit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    guild_id,
                    channel_id,
                    user_id,
                    discord_message_id,
                    query_text,
                    answer_text,
                    digest,
                    model,
                    sources_json,
                    latency_ms,
                    1 if cache_hit else 0,
                ),
            )
            self._conn.commit()

    def forget_user(self, user_id: str) -> int:
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET query_text = NULL, answer_text = NULL WHERE user_id = ?",
                (user_id,),
            )
            deleted = self._conn.execute(
                "DELETE FROM messages WHERE user_id = ?", (user_id,)
            ).rowcount
            self._conn.commit()
            if deleted:
                self._conn.execute("VACUUM")
            return int(deleted)

    def purge(self) -> int:
        with self._lock:
            count = self._count("SELECT COUNT(*) FROM messages")
            self._conn.execute("UPDATE messages SET query_text = NULL, answer_text = NULL")
            self._conn.execute("DELETE FROM messages")
            self._conn.execute("DELETE FROM rate_events")
            self._conn.commit()
            self._conn.execute("VACUUM")
            return count

    def message_texts(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT query_text, answer_text FROM messages").fetchall()
        texts: list[str] = []
        for row in rows:
            if row["query_text"]:
                texts.append(str(row["query_text"]))
            if row["answer_text"]:
                texts.append(str(row["answer_text"]))
        return texts

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    guild_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    discord_message_id TEXT,
                    query_text TEXT,
                    answer_text TEXT,
                    query_hash TEXT,
                    model TEXT,
                    sources_json TEXT,
                    latency_ms INTEGER,
                    cache_hit INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS rate_events (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_hash_time
                    ON messages(query_hash, created_at);
                CREATE INDEX IF NOT EXISTS idx_messages_user
                    ON messages(user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_rate_user
                    ON rate_events(user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_rate_channel
                    ON rate_events(channel_id, created_at);
                """
            )
            self._conn.commit()

    def _count(self, sql: str, params: tuple = ()) -> int:
        row = self._conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

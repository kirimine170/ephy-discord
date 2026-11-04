"""Karte Context Protocol v1 client.

Transport matches Karte `internal/contextcore` and ephy-runtime
`packages/karte_core/context.py`: a JSON request is written under
`.mdsys/context/v1/requests`, and the response is read from
`.mdsys/context/v1/responses`. Karte hashes the raw request bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ephy_discord.config import AppConfig, KarteConfig, resolved_karte_dir

PROTOCOL_VERSION = "1.0"
_MAX_RESPONSE_BYTES = 3 * 1024 * 1024
_SENSITIVITY_RANK = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
_EXCERPT_CHARS = 6000
_TOTAL_CHARS = 12_000


class KarteError(RuntimeError):
    pass


class KarteTimeout(KarteError):
    pass


class KarteProtocolError(KarteError):
    pass


@dataclass(frozen=True)
class SourceRef:
    doc_id: str
    title: str
    relative_path: str
    excerpt: str
    sha256: str

    def audit_dict(self) -> dict[str, str]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "relative_path": self.relative_path,
        }


@dataclass(frozen=True)
class ContextPack:
    status: str
    sources: tuple[SourceRef, ...]


class StubKarteClient:
    """Used when Karte is not configured or the spool cannot be opened."""

    def search_and_read(self, query: str) -> ContextPack:
        del query
        return ContextPack(status="unavailable", sources=())

    def search(self, query: str) -> dict:
        del query
        return {"status": "unavailable", "results": []}

    def read_document(self, doc_id: str) -> dict:
        del doc_id
        return {"status": "unavailable", "document": None}


class FilesystemKarteClient:
    def __init__(
        self,
        data_dir: str | Path,
        karte: KarteConfig,
        *,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ) -> None:
        self.karte = karte
        root = Path(data_dir).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("KARTE_DATA_DIR must be a directory")
        self.data_root = root
        self.requests_dir = self._checked(Path(".mdsys/context/v1/requests"))
        self.responses_dir = self._checked(Path(".mdsys/context/v1/responses"))
        for directory in (self.requests_dir, self.responses_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._sleep = sleep
        self._monotonic = monotonic

    def search(self, query: str) -> dict:
        text = " ".join(query.split())
        if not text:
            return {"status": "ok", "results": []}
        try:
            response = self._exchange(
                operation="search",
                query={"text": text[:2048], "top_k": self.karte.top_k},
                doc_id=None,
            )
        except KarteError:
            return {"status": "unavailable", "results": []}
        if response.get("status") != "ok":
            return {"status": "unavailable", "results": []}
        results = []
        for item in response.get("results") or []:
            if not isinstance(item, dict) or not _item_in_scope(item, self.karte):
                return {"status": "unavailable", "results": []}
            results.append(
                {
                    "doc_id": str(item.get("doc_id") or ""),
                    "title": str(item.get("title") or ""),
                    "relative_path": str(item.get("relative_path") or ""),
                    "snippet": str(item.get("snippet") or "")[:2048],
                    "score": item.get("score") or 0,
                }
            )
        return {"status": "ok", "results": results}

    def read_document(self, doc_id: str) -> dict:
        identifier = doc_id.strip()
        if not identifier:
            return {"status": "unavailable", "document": None}
        try:
            response = self._exchange(operation="read", query=None, doc_id=identifier)
        except KarteError:
            return {"status": "unavailable", "document": None}
        document = response.get("document") if response.get("status") == "ok" else None
        if not isinstance(document, dict) or not _item_in_scope(document, self.karte):
            return {"status": "unavailable", "document": None}
        body = str(document.get("body") or "")[:_EXCERPT_CHARS]
        return {
            "status": "ok",
            "document": {
                "doc_id": str(document.get("doc_id") or ""),
                "title": str(document.get("title") or ""),
                "relative_path": str(document.get("relative_path") or ""),
                "body": body,
            },
        }

    def search_and_read(self, query: str) -> ContextPack:
        text = " ".join(query.split())
        if not text:
            return ContextPack(status="ok", sources=())
        try:
            search = self._exchange(
                operation="search",
                query={"text": text[:2048], "top_k": self.karte.top_k},
                doc_id=None,
            )
        except KarteError:
            return ContextPack(status="unavailable", sources=())
        status = str(search.get("status") or "")
        if status == "not_found":
            return ContextPack(status="ok", sources=())
        if status != "ok":
            return ContextPack(status="unavailable", sources=())
        results = search.get("results") or []
        if not isinstance(results, list):
            return ContextPack(status="unavailable", sources=())

        selected: list[SourceRef] = []
        remaining = _TOTAL_CHARS
        for result in results[: self.karte.max_documents]:
            if remaining <= 0 or not isinstance(result, dict):
                break
            if not _item_in_scope(result, self.karte):
                return ContextPack(status="unavailable", sources=())
            limit = min(_EXCERPT_CHARS, remaining)
            excerpt = str(result.get("snippet") or "")[:limit]
            try:
                read = self._exchange(
                    operation="read", query=None, doc_id=str(result.get("doc_id") or "")
                )
                document = read.get("document") if read.get("status") == "ok" else None
                if isinstance(document, dict) and document.get("doc_id") == result.get("doc_id"):
                    if not _item_in_scope(document, self.karte):
                        return ContextPack(status="unavailable", sources=())
                    excerpt = _bounded_excerpt(str(document.get("body") or ""), excerpt, limit)
                    result = {**result, **{key: document[key] for key in document if key != "body"}}
            except KarteTimeout:
                pass
            except KarteError:
                return ContextPack(status="unavailable", sources=())
            source = _source_from(result, excerpt)
            if source is None:
                continue
            selected.append(source)
            remaining -= len(source.excerpt)
        return ContextPack(status="ok", sources=tuple(selected))

    def _exchange(self, *, operation: str, query: dict | None, doc_id: str | None) -> dict:
        request_id = f"ephy-discord-{operation}-{uuid.uuid4().hex}"
        payload = {
            "actor": {"id": self.karte.actor_id, "type": "ephy"},
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "doc_id": doc_id,
            "operation": operation,
            "protocol_version": PROTOCOL_VERSION,
            "query": query,
            "request_id": request_id,
            "scope": {
                "projects": list(self.karte.projects),
                "sensitivity_ceiling": self.karte.sensitivity_ceiling,
                "tags": list(self.karte.tags),
            },
        }
        data = canonical_json(payload)
        digest = hashlib.sha256(data).hexdigest()
        request_path = self.requests_dir / f"{request_id}.json"
        response_path = self.responses_dir / f"{request_id}.json"
        _atomic_write(request_path, data)
        deadline = self._monotonic() + self.karte.timeout_seconds
        while self._monotonic() < deadline:
            if response_path.exists():
                return self._read_response(response_path, request_id, digest, operation, doc_id)
            self._sleep(self.karte.poll_interval_seconds)
        raise KarteTimeout("context request timed out")

    def _read_response(
        self,
        path: Path,
        request_id: str,
        digest: str,
        operation: str,
        doc_id: str | None,
    ) -> dict:
        if path.is_symlink() or not path.is_file():
            raise KarteProtocolError("context response must be a regular file")
        self._assert_within(path)
        if path.stat().st_size > _MAX_RESPONSE_BYTES:
            raise KarteProtocolError("context response exceeds the size limit")
        try:
            response = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise KarteProtocolError("context response is invalid") from exc
        if not isinstance(response, dict):
            raise KarteProtocolError("context response is invalid")
        if response.get("protocol_version") != PROTOCOL_VERSION:
            raise KarteProtocolError("context response protocol is unsupported")
        if response.get("request_id") != request_id or response.get("request_sha256") != digest:
            raise KarteProtocolError("context response does not match the request")
        if response.get("operation") not in {operation, "invalid"}:
            raise KarteProtocolError("context response operation is invalid")
        document = response.get("document")
        if operation == "read" and isinstance(document, dict) and document.get("doc_id") != doc_id:
            raise KarteProtocolError("context response document does not match doc_id")
        return response

    def _checked(self, relative: Path) -> Path:
        candidate = self.data_root / relative
        self._assert_within(candidate)
        return candidate

    def _assert_within(self, candidate: Path) -> None:
        try:
            candidate.resolve(strict=False).relative_to(self.data_root)
        except ValueError as exc:
            raise ValueError("context protocol path escapes KARTE_DATA_DIR") from exc


def build_karte_client(config: AppConfig) -> StubKarteClient | FilesystemKarteClient:
    if config.karte.mode == "stub":
        return StubKarteClient()
    data_dir = resolved_karte_dir(config)
    if not data_dir:
        return StubKarteClient()
    try:
        return FilesystemKarteClient(data_dir, config.karte)
    except (OSError, ValueError):
        if config.karte.mode == "filesystem":
            return StubKarteClient()
        return StubKarteClient()


def canonical_json(payload: dict) -> bytes:
    """Same byte layout ephy-runtime uses before SHA-256."""

    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _source_from(result: dict, excerpt: str) -> SourceRef | None:
    doc_id = str(result.get("doc_id") or "")
    title = str(result.get("title") or "")
    relative_path = str(result.get("relative_path") or "")
    sha = str(result.get("sha256") or "")
    if not doc_id or not title:
        return None
    return SourceRef(
        doc_id=doc_id,
        title=title,
        relative_path=relative_path,
        excerpt=excerpt,
        sha256=sha,
    )


def _item_in_scope(item: dict, karte: KarteConfig) -> bool:
    project = str(item.get("project") or "")
    requested = {value.casefold() for value in karte.projects}
    if requested and "*" not in requested and project.casefold() not in requested:
        return False
    item_tags = {str(tag).casefold() for tag in item.get("tags") or []}
    requested_tags = {tag.casefold() for tag in karte.tags}
    if not requested_tags.issubset(item_tags):
        return False
    sensitivity = str(item.get("sensitivity") or "")
    if (
        sensitivity not in _SENSITIVITY_RANK
        or _SENSITIVITY_RANK[sensitivity] > _SENSITIVITY_RANK[karte.sensitivity_ceiling]
    ):
        return False
    return True


def _bounded_excerpt(body: str, snippet: str, limit: int) -> str:
    if len(body) <= limit:
        return body
    needle = snippet.strip().strip("….")
    match_at = body.casefold().find(needle.casefold()) if needle else -1
    if match_at < 0:
        return body[:limit]
    midpoint = match_at + len(needle) // 2
    start = max(0, min(midpoint - limit // 2, len(body) - limit))
    return body[start : start + limit]


def _atomic_write(destination: Path, data: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_path = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temp_path.open("xb") as handle:
            os.chmod(temp_path, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass

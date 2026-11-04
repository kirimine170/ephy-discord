from __future__ import annotations

import hashlib
import json
import threading
import time

from ephy_discord.config import KarteConfig
from ephy_discord.karte import (
    FilesystemKarteClient,
    KarteProtocolError,
    StubKarteClient,
    build_karte_client,
    canonical_json,
)
from tests.conftest import make_config


def test_stub_is_used_when_karte_is_not_reachable(tmp_path) -> None:
    config = make_config(tmp_path, karte_mode="stub")
    client = build_karte_client(config)
    assert isinstance(client, StubKarteClient)
    assert client.search_and_read("anything").status == "unavailable"

    missing = make_config(tmp_path, karte_mode="auto")
    assert isinstance(build_karte_client(missing), StubKarteClient)


def test_search_request_matches_context_protocol_v1(tmp_path) -> None:
    data = tmp_path / "karte"
    data.mkdir()
    client = _client(data, timeout_seconds=0.2)
    pack = client.search_and_read("personal context boundary")
    assert pack.status == "unavailable"
    written = list((data / ".mdsys/context/v1/requests").glob("*.json"))
    assert len(written) == 1
    raw = written[0].read_bytes()
    payload = json.loads(raw)
    assert payload["protocol_version"] == "1.0"
    assert payload["operation"] == "search"
    assert payload["doc_id"] is None
    assert payload["query"] == {"text": "personal context boundary", "top_k": 5}
    assert payload["actor"] == {"type": "ephy", "id": "ephy-discord"}
    assert payload["scope"]["sensitivity_ceiling"] == "internal"
    assert raw == canonical_json(payload)


def test_filesystem_roundtrip_reads_ranked_document(tmp_path) -> None:
    data = tmp_path / "karte"
    data.mkdir()
    client = _client(data, timeout_seconds=2)
    thread = threading.Thread(target=_answer_requests, args=(data, False), daemon=True)
    thread.start()
    pack = client.search_and_read("personal context boundary")
    thread.join(timeout=3)

    assert pack.status == "ok"
    assert len(pack.sources) == 1
    source = pack.sources[0]
    assert source.doc_id == "doc:synthetic-context-001"
    assert source.title == "Synthetic context boundary"
    assert "Ephy uses it as a client." in source.excerpt


def test_response_hash_mismatch_is_not_used(tmp_path) -> None:
    data = tmp_path / "karte"
    data.mkdir()
    client = _client(data, timeout_seconds=2)
    thread = threading.Thread(target=_answer_requests, args=(data, True), daemon=True)
    thread.start()
    pack = client.search_and_read("personal context boundary")
    thread.join(timeout=3)
    assert pack.status == "unavailable"


def test_read_response_rejects_a_different_hash(tmp_path) -> None:
    data = tmp_path / "karte"
    data.mkdir()
    client = _client(data)
    request_id = "ephy-discord-search-abc"
    path = client.responses_dir / f"{request_id}.json"
    path.write_text(
        json.dumps(
            {
                "protocol_version": "1.0",
                "request_id": request_id,
                "request_sha256": "b" * 64,
                "operation": "search",
                "status": "ok",
                "results": [],
            }
        ),
        encoding="utf-8",
    )
    try:
        client._read_response(path, request_id, "a" * 64, "search", None)
    except KarteProtocolError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("mismatched response was accepted")


def _client(data, timeout_seconds: float = 1) -> FilesystemKarteClient:
    return FilesystemKarteClient(data, _karte_config(timeout_seconds))


def _karte_config(timeout_seconds: float) -> KarteConfig:
    return KarteConfig(
        mode="filesystem",
        data_dir="",
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=0.01,
        projects=(),
        tags=(),
        sensitivity_ceiling="internal",
        top_k=5,
        max_documents=3,
        actor_id="ephy-discord",
    )


def _answer_requests(data, bad_hash: bool) -> None:
    requests = data / ".mdsys/context/v1/requests"
    responses = data / ".mdsys/context/v1/responses"
    seen: set[str] = set()
    deadline = time.time() + 2.5
    while time.time() < deadline and len(seen) < 2:
        if requests.exists():
            for path in requests.glob("*.json"):
                if path.name in seen:
                    continue
                raw = path.read_bytes()
                body = json.loads(raw)
                seen.add(path.name)
                digest = "0" * 64 if bad_hash else hashlib.sha256(raw).hexdigest()
                payload = _response(body, digest)
                (responses / path.name).write_text(json.dumps(payload), encoding="utf-8")
        time.sleep(0.01)


def _response(body: dict, digest: str) -> dict:
    common = {
        "protocol_version": "1.0",
        "request_id": body["request_id"],
        "request_sha256": digest,
        "operation": body["operation"],
        "status": "ok",
        "diagnostics": [],
        "error": None,
        "processed_at": "2026-11-04T00:00:01Z",
    }
    if body["operation"] == "search":
        common["results"] = [
            {
                "doc_id": "doc:synthetic-context-001",
                "title": "Synthetic context boundary",
                "project": "ephy",
                "kind": "decision",
                "tags": ["architecture"],
                "sensitivity": "internal",
                "relative_path": "content/projects/ephy/decision/2026-09/synthetic-context.md",
                "updated_at": "2026-09-01T00:00:00Z",
                "sha256": "a" * 64,
                "snippet": "Karte owns durable Personal Context.",
                "score": 12,
                "provenance": [],
            }
        ]
        common["document"] = None
        return common
    common["results"] = []
    common["document"] = {
        "doc_id": "doc:synthetic-context-001",
        "title": "Synthetic context boundary",
        "project": "ephy",
        "kind": "decision",
        "tags": ["architecture"],
        "sensitivity": "internal",
        "relative_path": "content/projects/ephy/decision/2026-09/synthetic-context.md",
        "updated_at": "2026-09-01T00:00:00Z",
        "sha256": "a" * 64,
        "body": "Karte owns durable Personal Context and Ephy uses it as a client.",
        "provenance": [],
    }
    return common

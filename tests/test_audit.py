from __future__ import annotations

import pytest

from ephy_discord.audit import AUDIT_FIELDS
from ephy_discord.service import Turn
from tests.conftest import make_service


@pytest.mark.asyncio
async def test_audit_records_request_metadata_without_message_text_or_secrets(tmp_path) -> None:
    service, _llm, _karte = make_service(tmp_path)
    question = "SENTINEL_QUERY_should_not_be_logged"
    await service.answer(Turn(guild_id=1, channel_id=10, user_id=42, text=question))
    service.audit.write(
        {
            "ts": "2026-11-04T00:00:00Z",
            "event": "answer",
            "user_id": "42",
            "query": question,
            "token": "super-secret-token",
            "sources": [
                {
                    "doc_id": "doc:1",
                    "title": "Title",
                    "relative_path": "content/a.md",
                    "excerpt": question,
                }
            ],
        }
    )
    raw = service.audit.path.read_text(encoding="utf-8")
    assert question not in raw
    assert "super-secret-token" not in raw
    for row in service.audit.read_lines():
        assert set(row) <= set(AUDIT_FIELDS)
        for source in row.get("sources", []):
            assert set(source) == {"doc_id", "title", "relative_path"}
    await service.aclose()

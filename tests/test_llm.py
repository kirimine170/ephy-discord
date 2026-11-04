from __future__ import annotations

import pytest

from ephy_discord.config import LlmConfig
from ephy_discord.karte import ContextPack
from ephy_discord.llm import LlmClient
from ephy_discord.mock_llm import MockLlmServer
from ephy_discord.service import KARTE_UNAVAILABLE_NOTE, Turn
from tests.conftest import FakeKarte, make_service


@pytest.mark.asyncio
async def test_client_talks_to_loopback_openai_endpoint() -> None:
    server = MockLlmServer(reply="<think>hidden</think>モックから返事です．")
    server.start()
    try:
        client = LlmClient(
            LlmConfig(
                base_url=server.base_url,
                model="mock",
                timeout_seconds=5,
                temperature=0.2,
                max_tokens=100,
                via_gateway=True,
                gateway_mode="fast",
            )
        )
        result = await client.complete(
            [
                {"role": "system", "content": "test"},
                {"role": "user", "content": "自己紹介して"},
            ]
        )
        await client.aclose()
    finally:
        server.close()

    assert result.text == "モックから返事です．"
    assert result.model == "mock"
    assert server.last_payload is not None
    assert server.last_payload["stream"] is False
    assert server.last_payload["metadata"] == {"mode": "fast", "web_search": False}
    assert "hidden" not in result.text


@pytest.mark.asyncio
async def test_answer_pipeline_uses_mock_when_karte_is_stubbed(tmp_path) -> None:
    server = MockLlmServer()
    server.start()
    service, _llm, karte = make_service(tmp_path)
    assert isinstance(karte, FakeKarte)
    karte.pack = ContextPack(status="unavailable", sources=())
    service.llm = LlmClient(
        LlmConfig(
            base_url=server.base_url,
            model="mock",
            timeout_seconds=5,
            temperature=0.7,
            max_tokens=100,
            via_gateway=False,
            gateway_mode="fast",
        )
    )
    try:
        result = await service.answer(
            Turn(guild_id=1, channel_id=10, user_id=1, text="自己紹介して")
        )
    finally:
        await service.aclose()
        server.close()

    assert result.kind == "answer"
    assert result.text.startswith("こんにちは．わたしはローカルのモックです．")
    assert KARTE_UNAVAILABLE_NOTE in result.text
    assert server.last_payload is not None
    assert "metadata" not in server.last_payload

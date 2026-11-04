from __future__ import annotations

from ephy_discord.text import extract_question, split_discord_text, strip_thinking, utf16_len


def test_extract_question_removes_only_this_bot() -> None:
    assert extract_question("<@42> 今日は？", 42) == "今日は？"
    assert extract_question("<@!42> <@99> 残す", 42) == "<@99> 残す"


def test_thinking_blocks_are_removed() -> None:
    assert strip_thinking("<think>secret</think>答えです．") == "答えです．"


def test_split_respects_utf16_units() -> None:
    text = "あ" * 4000
    chunks = split_discord_text(text, limit=1900)
    assert len(chunks) == 3
    assert "".join(chunks) == text
    assert all(utf16_len(chunk) <= 1900 for chunk in chunks)
    emoji = "🙂" * 2000
    pieces = split_discord_text(emoji, limit=100)
    assert "".join(pieces) == emoji
    assert all(utf16_len(piece) <= 100 for piece in pieces)

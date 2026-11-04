"""Discord-safe text helpers. Comments are English; user-facing strings live with callers."""

from __future__ import annotations

import re

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN = re.compile(r"<think>.*\Z", re.IGNORECASE | re.DOTALL)
_MENTION = re.compile(r"<@!?(\d+)>")

# Discord counts UTF-16 code units and rejects messages over 2000.
DISCORD_UTF16_LIMIT = 1900


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def strip_thinking(text: str) -> str:
    """Drop chain-of-thought blocks some local models echo into content."""

    cleaned = _THINK_BLOCK.sub("", text)
    cleaned = _THINK_OPEN.sub("", cleaned)
    return cleaned.strip()


def extract_question(content: str, bot_user_id: int) -> str:
    """Remove this bot's mention and collapse whitespace."""

    def _replace(match: re.Match[str]) -> str:
        if int(match.group(1)) == bot_user_id:
            return " "
        return match.group(0)

    without_self = _MENTION.sub(_replace, content or "")
    return " ".join(without_self.split())


def split_discord_text(text: str, limit: int = DISCORD_UTF16_LIMIT) -> list[str]:
    """Split on paragraph boundaries so each chunk fits Discord's limit."""

    normalized = (text or "").strip()
    if not normalized:
        return []
    if utf16_len(normalized) <= limit:
        return [normalized]

    chunks: list[str] = []
    current = ""
    for paragraph in re.split(r"\n{2,}", normalized):
        piece = paragraph.strip()
        if not piece:
            continue
        if utf16_len(piece) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_hard_split(piece, limit))
            continue
        candidate = piece if not current else f"{current}\n\n{piece}"
        if utf16_len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


def _hard_split(text: str, limit: int) -> list[str]:
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for char in text:
        units = utf16_len(char)
        if current and size + units > limit:
            pieces.append("".join(current))
            current = [char]
            size = units
        else:
            current.append(char)
            size += units
    if current:
        pieces.append("".join(current))
    return pieces

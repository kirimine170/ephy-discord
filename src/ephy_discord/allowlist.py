"""Guild and channel allowlist. Empty channel lists fail closed."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Allowlist:
    guild_id: int
    channel_ids: frozenset[int]

    def permits(self, guild_id: int | None, channel_id: int | None) -> bool:
        if guild_id is None or channel_id is None:
            return False
        if not self.channel_ids:
            return False
        return guild_id == self.guild_id and channel_id in self.channel_ids

"""Minimal Discord intents and bot permission bits for the O-1 read-only bot."""

from __future__ import annotations

# Gateway intent bits. discord.py names are checked in tests against the library.
INTENT_GUILDS = 1 << 0
INTENT_GUILD_MESSAGES = 1 << 9
INTENT_MESSAGE_CONTENT = 1 << 15

# Bot permission bits. Invite URL uses only this set.
PERM_VIEW_CHANNEL = 1 << 10
PERM_SEND_MESSAGES = 1 << 11
PERM_READ_MESSAGE_HISTORY = 1 << 16
PERM_USE_APPLICATION_COMMANDS = 1 << 31

MINIMAL_PERMISSIONS: dict[str, int] = {
    "view_channel": PERM_VIEW_CHANNEL,
    "send_messages": PERM_SEND_MESSAGES,
    "read_message_history": PERM_READ_MESSAGE_HISTORY,
    "use_application_commands": PERM_USE_APPLICATION_COMMANDS,
}


def minimal_permission_value() -> int:
    value = 0
    for bit in MINIMAL_PERMISSIONS.values():
        value |= bit
    return value

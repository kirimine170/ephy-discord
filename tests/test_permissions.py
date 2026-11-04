from __future__ import annotations

from ephy_discord.bot import build_intents, expected_intent_value, minimal_permissions
from ephy_discord.permissions import minimal_permission_value


def test_intents_are_only_guilds_messages_and_content() -> None:
    intents = build_intents()
    assert intents.value == expected_intent_value()
    assert intents.guilds is True
    assert intents.guild_messages is True
    assert intents.message_content is True
    assert intents.members is False
    assert intents.presences is False
    assert intents.dm_messages is False


def test_bot_permissions_are_the_minimum_needed_to_reply() -> None:
    permissions = minimal_permissions()
    assert permissions.value == minimal_permission_value()
    assert permissions.view_channel is True
    assert permissions.send_messages is True
    assert permissions.read_message_history is True
    assert permissions.use_application_commands is True
    assert permissions.administrator is False
    assert permissions.manage_messages is False
    assert permissions.manage_channels is False
    assert permissions.mention_everyone is False

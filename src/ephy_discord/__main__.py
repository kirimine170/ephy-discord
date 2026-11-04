"""Command line entry for check, one-shot answer, mock LLM, and the bot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from ephy_discord import __version__
from ephy_discord.bot import EphyDiscordBot, build_intents, minimal_permissions
from ephy_discord.config import (
    MCP_BEARER_ENV,
    MCP_OAUTH_ENV,
    TOKEN_ENV,
    load_config,
    read_bot_token,
    resolved_karte_dir,
)
from ephy_discord.mock_llm import serve
from ephy_discord.permissions import minimal_permission_value
from ephy_discord.service import Turn, build_service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ephy-discord")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="validate config without connecting")
    check.add_argument("--config", required=True)

    answer = sub.add_parser("answer", help="answer one question via the local LLM")
    answer.add_argument("--config", required=True)
    answer.add_argument("--text", required=True)

    run = sub.add_parser("run", help="connect to Discord")
    run.add_argument("--config", required=True)

    mock = sub.add_parser("mock-llm", help="serve a loopback chat completion mock")
    mock.add_argument("--host", default="127.0.0.1")
    mock.add_argument("--port", type=int, default=8765)

    args = parser.parse_args(argv)
    if args.command == "mock-llm":
        serve(args.host, args.port)
        return 0
    if args.command == "check":
        return _check(args.config)
    if args.command == "answer":
        return asyncio.run(_answer(args.config, args.text))
    if args.command == "run":
        return _run(args.config)
    return 2


def _check(path: str) -> int:
    config = load_config(path)
    token_set = bool(os.environ.get(TOKEN_ENV, "").strip())
    karte_dir = resolved_karte_dir(config)
    intents = build_intents()
    print(f"ephy-discord {__version__}")
    print(f"guild_id={config.guild_id}")
    print(f"allowed_channels={len(config.allowed_channel_ids)}")
    print(
        f"llm={config.llm.base_url} model={config.llm.model} via_gateway={config.llm.via_gateway}"
    )
    print(f"karte_mode={config.karte.mode} karte_dir_set={bool(karte_dir)}")
    print(
        f"mcp_enabled={config.mcp.enabled} mcp_port={config.mcp.port} "
        f"approvers={len(config.approver_user_ids)}"
    )
    print(f"{MCP_BEARER_ENV}={'set' if os.environ.get(MCP_BEARER_ENV, '').strip() else 'unset'}")
    print(f"{MCP_OAUTH_ENV}={'set' if os.environ.get(MCP_OAUTH_ENV, '').strip() else 'unset'}")
    print(f"intents={intents.value} permissions={minimal_permissions().value}")
    print(f"permission_bits={minimal_permission_value()}")
    print(f"{TOKEN_ENV}={'set' if token_set else 'unset'}")
    if not config.allowed_channel_ids:
        print("allowlist is empty; the bot will not answer mentions or /ephy ask")
    return 0


async def _answer(path: str, text: str) -> int:
    config = load_config(path)
    service = build_service(config)
    try:
        result = await service.answer(
            Turn(
                guild_id=config.guild_id,
                channel_id=0,
                user_id=0,
                text=text,
                bypass_allowlist=True,
            )
        )
    finally:
        await service.aclose()
    if result.kind in {"answer", "cached"}:
        print(result.text)
        return 0
    print(result.kind, file=sys.stderr)
    return 1


def _run(path: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    config = load_config(path)
    try:
        token = read_bot_token()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    service = build_service(config)
    bot = EphyDiscordBot(config, service)
    bot.run(token, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

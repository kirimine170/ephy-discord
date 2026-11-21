"""Pinned backends；existing authenticated run/turns and bounded Codex execution．"""

from __future__ import annotations

import asyncio
import configparser
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ephy_discord.parliament import (
    ParliamentError,
    Policy,
    Request,
    exact,
    unlinked,
)


def pin(path: str, digest: str) -> Path:
    file = unlinked(path)
    if (
        not re.fullmatch(r"[a-f0-9]{64}", digest)
        or not file.is_file()
        or (hashlib.sha256(file.read_bytes()).hexdigest() != digest)
    ):
        raise ParliamentError("executor_pin_changed")
    return file


def clean(text: str) -> str:
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text) > 6000
        or any(ord(c) < 32 and c not in "\n\t" for c in text)
    ):
        raise ParliamentError("invalid_backend_result")
    if re.search(r"Bearer\s+\S+|\b(?:sk-|gh[pousr]_|eyJ)[A-Za-z0-9_.-]{15,}", text):
        raise ParliamentError("backend_result_contains_secret_pattern")
    return text


def safe_git_source(source: Path) -> None:
    """No includes，hooks，fsmonitor，filters or external Git helpers before OS isolation．"""
    metadata = unlinked(source / ".git")
    if (
        not metadata.is_dir()
        or (metadata / "commondir").exists()
        or (metadata / "config.worktree").exists()
    ):
        raise ParliamentError("primary_git_checkout_required")
    file = unlinked(metadata / "config")
    if file.stat().st_size > 65536 or file.stat().st_nlink != 1:
        raise ParliamentError("unsafe_git_configuration")
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(file.read_text(encoding="utf-8"))
    except (ValueError, configparser.Error):
        raise ParliamentError("unsafe_git_configuration") from None
    if parser.defaults():
        raise ParliamentError("unsafe_git_configuration")
    core = {
        "repositoryformatversion",
        "filemode",
        "bare",
        "logallrefupdates",
        "ignorecase",
        "precomposeunicode",
        "symlinks",
        "protectntfs",
    }
    for section in parser.sections():
        allowed = (
            core
            if section == "core"
            else {"url", "fetch"}
            if re.fullmatch(r'remote "[A-Za-z0-9_-]+"', section)
            else {"remote", "merge"}
            if re.fullmatch(r'branch "[A-Za-z0-9_/-]+"', section)
            else {"name", "email"}
            if section == "user"
            else set()
        )
        if not set(parser[section]).issubset(allowed) or not allowed:
            raise ParliamentError("execution_capable_git_configuration_denied")
    if (
        parser.get("core", "bare", fallback="false") != "false"
        or parser.get("core", "repositoryformatversion", fallback="0") != "0"
    ):
        raise ParliamentError("unsupported_git_repository")


def existing_endpoint(policy):
    endpoint = policy.bots[policy.self_name]["conversation_url"]
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or not parsed.port
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/v1/parliament"
    ):
        raise ParliamentError("existing_agent_backend_unconfigured")
    return endpoint


class ExistingConversation:
    def __init__(self, *, transport=None, clock=time.monotonic, sleep=asyncio.sleep):
        self.transport, self.clock, self.sleep = transport, clock, sleep

    async def owns_reply(self, event, policy):
        endpoint = existing_endpoint(policy)
        identity = {
            "version": 1,
            "guild_id": str(policy.guild),
            "channel_id": str(event.channel),
            "recipient_id": str(policy.self_id),
            "reply_to": str(event.reply_to),
        }
        async with httpx.AsyncClient(
            trust_env=False, follow_redirects=False, timeout=10, transport=self.transport
        ) as client:
            async with client.stream(
                "GET",
                endpoint,
                params={k: identity[k] for k in ("channel_id", "recipient_id", "reply_to")},
            ) as response:
                if response.status_code != 200:
                    raise ParliamentError("backend_reply_provenance_unavailable")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 2048:
                        raise ParliamentError("backend_reply_provenance_unavailable")
        result = exact(json.loads(body), {*identity, "owned"})
        if (
            any(type(result[k]) is not type(v) or result[k] != v for k, v in identity.items())
            or type(result["owned"]) is not bool
        ):
            raise ParliamentError("backend_reply_identity_mismatch")
        return result["owned"]

    async def execute(self, request: Request, policy: Policy, job_id: str) -> str:
        if request.kind == "implement":
            raise ParliamentError("implementation_adapter_unconfigured")
        if len(request.text) > 1500:
            raise ParliamentError("existing_conversation_text_limit")
        endpoint = existing_endpoint(policy)
        event = request.event
        identity = {
            "version": 1,
            "job_id": job_id,
            "guild_id": str(event.guild),
            "channel_id": str(event.channel),
            "requester_id": str(event.author),
            "recipient_id": str(policy.self_id),
            "kind": request.kind,
            "message_id": str(event.message),
        }

        async def receive(client, method, **kwargs):
            try:
                async with client.stream(method, endpoint, **kwargs) as response:
                    if method == "GET" and (
                        response.status_code == 429 or response.status_code >= 500
                    ):
                        return None
                    if response.status_code != 200:
                        raise ParliamentError("existing_agent_backend_rejected")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 8192:
                            raise ParliamentError("backend_result_too_large")
            except httpx.TransportError:
                if method == "GET":
                    return None
                raise
            result = exact(
                json.loads(body),
                {
                    *identity,
                    "status",
                    "run_id",
                    "turn_id",
                    "authenticated_answer",
                    "discord_display",
                    "message_url",
                },
            )
            if any(type(result[k]) is not type(v) or result[k] != v for k, v in identity.items()):
                raise ParliamentError("backend_identity_mismatch")
            if (
                result["status"] not in {"pending", "completed", "unavailable"}
                or type(result["authenticated_answer"]) is not bool
                or result["discord_display"] not in {"pending", "posted", "unknown"}
                or not isinstance(result["run_id"], str)
                or not isinstance(result["turn_id"], str)
            ):
                raise ParliamentError("invalid_backend_result")
            return result

        async with httpx.AsyncClient(
            trust_env=False, follow_redirects=False, timeout=10, transport=self.transport
        ) as client:
            deadline = self.clock() + 100
            # One start only；an ambiguous POST is never retried．Only GET may poll．
            result = await receive(client, "POST", json={**identity, "text": request.text})
            run, turn = result["run_id"], result["turn_id"]
            while result["status"] == "pending":
                if self.clock() >= deadline:
                    raise RuntimeError("existing_run_result_unconfirmed_no_retry")
                await self.sleep(min(1, deadline - self.clock()))
                observed = await receive(client, "GET", params={"job_id": job_id})
                if observed is None:
                    continue
                result = observed
                if (result["run_id"], result["turn_id"]) != (run, turn):
                    raise ParliamentError("backend_run_identity_changed")
            if (
                result["status"] != "completed"
                or result["authenticated_answer"] is not True
                or result["discord_display"] != "posted"
            ):
                raise ParliamentError("existing_run_not_completed")
            url = result["message_url"]
            if not isinstance(url, str) or not re.fullmatch(
                rf"https://discord\.com/channels/{event.guild}/{event.channel}/[1-9][0-9]{{0,29}}",
                url,
            ):
                raise ParliamentError("backend_delivery_destination_mismatch")
            # The normal orchestrator outbox already posts the authenticated answer．
            # Return a receipt，not a second copy or a reconstructed persona answer．
            return "既存の認証済みrun／turnから同じスレッドへ回答を中継しました．\n" + url


async def command(argv: list[str], *, cwd: Path, env: dict, timeout=100, stdin=b"") -> bytes:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    try:
        # Bound output as it arrives；do not persist model stream or stderr．
        async def collect():
            output = bytearray()
            while chunk := await process.stdout.read(65536):
                output.extend(chunk)
                if len(output) > 2 * 1024 * 1024:
                    raise ParliamentError("executor_output_too_large")
            return bytes(output)

        async with asyncio.timeout(timeout):
            process.stdin.write(stdin)
            await process.stdin.drain()
            process.stdin.close()
            output = await collect()
            if await process.wait() != 0:
                raise ParliamentError("executor_failed_no_retry")
            return output
    finally:
        if process.returncode is None:
            if os.name == "nt":
                stop = await asyncio.create_subprocess_exec(
                    str(Path(os.environ["SystemRoot"]) / "System32/taskkill.exe"),
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=0x08000000,
                )
                await stop.wait()
            else:
                process.kill()
            await process.wait()


class CodexProposal:
    def __init__(self, directory):
        self.directory = directory

    async def execute(self, request: Request, policy: Policy, job_id: str) -> str:
        from ephy_discord.parliament_codex import CodexPipeline

        return await CodexPipeline(self.directory).execute(request, policy, job_id)


class RegisteredBackend:
    def __init__(self, directory):
        self.directory = directory

    async def owns_reply(self, event, policy):
        if policy.bots[policy.self_name]["backend"] != "existing":
            return False
        return await ExistingConversation().owns_reply(event, policy)

    async def execute(self, request: Request, policy: Policy, job_id: str) -> str:
        backend = (
            CodexProposal(self.directory)
            if request.kind == "implement" or policy.bots[policy.self_name]["backend"] == "codex"
            else ExistingConversation()
        )
        return await backend.execute(request, policy, job_id)

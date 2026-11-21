"""Opt-in parliament admission，durable execution and at-most-once delivery．"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

RESULT_PREFIX = "[parliament-result v1 "


class ParliamentError(ValueError):
    """Only fixed error codes may cross the Discord boundary．"""


def exact(value: object, keys: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ParliamentError("invalid_policy_fields")
    return value


def snowflake(value: object) -> int:
    if type(value) is not int or not 0 < value < 2**64:
        raise ParliamentError("invalid_discord_id")
    return value


def unlinked(path: str | Path) -> Path:
    result = Path(path)
    if not result.is_absolute() or any(
        item.is_symlink() or item.is_junction() for item in (result, *result.parents)
    ):
        raise ParliamentError("unsafe_policy_path")
    return result.resolve()


def object_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ParliamentError("duplicate_policy_key")
        result[key] = value
    return result


@dataclass(frozen=True)
class Policy:
    path: Path
    digest: str
    guild: int
    channel: int
    developer_role: int
    self_name: str
    bots: dict
    repositories: dict
    state_dir: Path
    codex: dict

    @property
    def self_id(self) -> int:
        return self.bots[self.self_name]["user_id"]


def load_policy(path: Path) -> Policy | None:
    path = unlinked(path)
    if path.stat().st_size > 32768:
        raise ParliamentError("policy_too_large")
    raw = path.read_bytes()
    value = json.loads(raw, object_pairs_hook=object_pairs)
    if value == {"version": 1, "enabled": False}:
        return None
    p = exact(
        value,
        {
            "version",
            "enabled",
            "guild_id",
            "channel_id",
            "developer_role_id",
            "self_bot",
            "bots",
            "repositories",
            "state_dir",
            "codex",
        },
    )
    if type(p["version"]) is not int or p["version"] != 1 or p["enabled"] is not True:
        raise ParliamentError("invalid_policy_version")
    if (
        not isinstance(p["bots"], dict)
        or not 2 <= len(p["bots"]) <= 32
        or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", key) for key in p["bots"])
    ):
        raise ParliamentError("invalid_registered_bot_registry")
    if len({name.casefold() for name in p["bots"]}) != len(p["bots"]):
        raise ParliamentError("case_ambiguous_bot_alias")
    if p["self_bot"] not in p["bots"]:
        raise ParliamentError("unregistered_self_bot")
    for bot in p["bots"].values():
        exact(bot, {"user_id", "application_id", "conversation_url", "backend"})
        if bot["backend"] not in {"codex", "existing"}:
            raise ParliamentError("invalid_registered_bot_backend")
        snowflake(bot["user_id"])
        snowflake(bot["application_id"])
        if not isinstance(bot["conversation_url"], str):
            raise ParliamentError("invalid_conversation_endpoint")
    if len({x["user_id"] for x in p["bots"].values()}) != len(p["bots"]) or len(
        {x["application_id"] for x in p["bots"].values()}
    ) != len(p["bots"]):
        raise ParliamentError("shared_bot_identity_forbidden")
    repos = p["repositories"]
    if not isinstance(repos, dict) or len(repos) > 32:
        raise ParliamentError("invalid_repository_registry")
    for name, repo in repos.items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise ParliamentError("invalid_repository_alias")
        exact(
            repo,
            {
                "path",
                "member_ids",
                "recipient_bots",
                "allowed_paths",
                "github_repository",
                "base_branch",
                "checks",
            },
        )
        if (
            not isinstance(repo["github_repository"], str)
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}",
                repo["github_repository"],
            )
            or repo["base_branch"] not in {"main", "master"}
        ):
            raise ParliamentError("invalid_publisher_repository")
        if not isinstance(repo["checks"], list) or not 1 <= len(repo["checks"]) <= 8:
            raise ParliamentError("fixed_checks_required")
        for check in repo["checks"]:
            exact(check, {"argv", "sha256"})
            if (
                not isinstance(check["argv"], list)
                or not check["argv"]
                or any(not isinstance(arg, str) for arg in check["argv"])
                or not re.fullmatch(r"[a-f0-9]{64}", check["sha256"])
            ):
                raise ParliamentError("invalid_fixed_check")
            unlinked(check["argv"][0])
        unlinked(repo["path"])
        if not isinstance(repo["member_ids"], list) or not repo["member_ids"]:
            raise ParliamentError("repository_members_required")
        for member in repo["member_ids"]:
            snowflake(member)
        if (
            not isinstance(repo["recipient_bots"], list)
            or not repo["recipient_bots"]
            or any(x not in p["bots"] for x in repo["recipient_bots"])
        ):
            raise ParliamentError("invalid_repository_recipients")
        if not isinstance(repo["allowed_paths"], list) or not repo["allowed_paths"]:
            raise ParliamentError("repository_paths_required")
        for item in repo["allowed_paths"]:
            if (
                not isinstance(item, str)
                or not re.fullmatch(r"[A-Za-z0-9_/-]+", item)
                or any(part in {"", ".", ".."} for part in item.rstrip("/").split("/"))
            ):
                raise ParliamentError("invalid_repository_scope")
    state = unlinked(p["state_dir"])
    code_root = Path(__file__).resolve().parents[2]
    if state.is_relative_to(code_root) or path.is_relative_to(code_root):
        raise ParliamentError("private_policy_and_state_must_be_outside_code")
    for repo in repos.values():
        source = unlinked(repo["path"])
        if path.is_relative_to(source):
            raise ParliamentError("policy_must_be_outside_registered_repositories")
        if state == source or state.is_relative_to(source) or source.is_relative_to(state):
            raise ParliamentError("repository_state_overlap")
    if not isinstance(p["codex"], dict):
        raise ParliamentError("invalid_codex_policy")
    if p["codex"] != {"enabled": False}:
        exact(
            p["codex"],
            {
                "enabled",
                "binary",
                "binary_sha256",
                "git",
                "git_sha256",
                "home",
                "read_roots",
                "denied_roots",
                "publish_draft_pr",
            },
        )
        if p["codex"]["enabled"] is not True:
            raise ParliamentError("invalid_codex_policy")
        if type(p["codex"]["publish_draft_pr"]) is not bool:
            raise ParliamentError("invalid_codex_policy")
        for name in ("read_roots", "denied_roots"):
            roots = p["codex"][name]
            if not isinstance(roots, list) or not 1 <= len(roots) <= 32:
                raise ParliamentError("invalid_sandbox_roots")
            for root in roots:
                unlinked(root)
    return Policy(
        path,
        hashlib.sha256(raw).hexdigest(),
        snowflake(p["guild_id"]),
        snowflake(p["channel_id"]),
        snowflake(p["developer_role_id"]),
        p["self_bot"],
        p["bots"],
        repos,
        state,
        p["codex"],
    )


@dataclass(frozen=True)
class Event:
    message: int
    guild: int
    channel: int
    author: int
    author_bot: bool
    content: str
    reply_to: int | None = None
    webhook: bool = False
    system: bool = False
    everyone: bool = False


@dataclass(frozen=True)
class Request:
    event: Event
    kind: str
    repository: str | None
    text: str
    notify: tuple[int, ...] = ()
    correlation: str | None = None


class Directory(Protocol):
    async def channel(self, identifier: int) -> dict: ...
    async def guild(self, identifier: int) -> dict: ...
    async def member(self, guild: int, identifier: int) -> dict: ...
    async def roles(self, guild: int) -> list[dict]: ...
    async def message(self, channel: int, identifier: int) -> dict: ...


class Backend(Protocol):
    async def execute(self, request: Request, policy: Policy, job_id: str) -> str: ...


def parse(event: Event, policy: Policy, *, own_reply: bool) -> Request | None:
    if event.content.startswith(RESULT_PREFIX) or len(event.content) > 4000:
        return None
    lines = event.content.split("\n", 1)
    header = re.fullmatch(
        rf"<@!?{policy.self_id}> (ask|opinion|implement)(?: ([a-z][a-z0-9_-]{{0,63}}))?"
        r"(?: notify=(<@!?\d+>(?:,<@!?\d+>){0,4}))?"
        r"(?: request=([a-zA-Z0-9_-]{8,64}) hop=(0))?",
        lines[0],
    )
    if header is None:
        if (
            own_reply
            and not event.author_bot
            and event.content.strip()
            and not event.content.startswith("<@")
        ):
            return Request(event, "ask", None, event.content.strip())
        return None
    kind, repo, notify, correlation, hop = header.groups()
    if (kind == "implement") != (repo is not None) or len(lines) != 2 or not lines[1].strip():
        return None
    if event.author_bot and (correlation is None or hop != "0"):
        return None
    return Request(
        event,
        kind,
        repo,
        lines[1].strip(),
        tuple(int(x) for x in re.findall(r"\d+", notify or "")),
        correlation,
    )


async def authorize(request: Request, policy: Policy, directory: Directory, *, delivery=False):
    event = request.event
    guild = await directory.guild(policy.guild)
    channel = await directory.channel(event.channel)
    if (
        int(guild["id"]) != policy.guild
        or event.guild != policy.guild
        or int(channel["id"]) != event.channel
        or int(channel["guild_id"]) != policy.guild
        or not (
            event.channel == policy.channel
            and channel["type"] == 0
            or channel["type"] == 11
            and int(channel.get("parent_id", 0)) == policy.channel
        )
        or channel.get("thread_metadata", {}).get("archived") is True
    ):
        raise ParliamentError("channel_scope_denied")
    member = await directory.member(event.guild, event.author)
    if (
        int(member["user"]["id"]) != event.author
        or bool(member["user"].get("bot", False)) != event.author_bot
        or member.get("pending", False)
    ):
        raise ParliamentError("member_identity_denied")
    registered = {x["user_id"] for x in policy.bots.values()}
    if event.author_bot and event.author not in registered:
        raise ParliamentError("unregistered_bot")
    if event.author_bot and event.reply_to is not None:
        parent = await directory.message(event.channel, event.reply_to)
        if (
            int(parent["id"]) != event.reply_to
            or int(parent["channel_id"]) != event.channel
            or parent["author"].get("bot", False)
            or parent.get("webhook_id") is not None
        ):
            raise ParliamentError("bot_reply_chain_denied")
    if not delivery and request.kind == "implement":
        roles = await directory.roles(event.guild)
        if policy.developer_role not in {int(r["id"]) for r in roles} or (
            policy.developer_role not in {int(r) for r in member.get("roles", [])}
        ):
            raise ParliamentError("developer_role_required")
        repo = policy.repositories.get(request.repository)
        if (
            repo is None
            or event.author not in repo["member_ids"]
            or (policy.self_name not in repo["recipient_bots"])
        ):
            raise ParliamentError("repository_access_denied")
        unlinked(repo["path"])
    for identifier in request.notify:
        person = await directory.member(event.guild, identifier)
        if int(person["user"]["id"]) != identifier or person["user"].get("bot", False):
            raise ParliamentError("human_notification_denied")


class Ledger:
    def __init__(self, policy: Policy):
        self.root = policy.state_dir
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / f"parliament-{policy.self_id}.sqlite3"
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY,digest TEXT,"
                "policy TEXT,payload TEXT,execution TEXT DEFAULT 'accepted',result TEXT,"
                "delivery TEXT DEFAULT 'pending',created REAL NOT NULL,origin_bot TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS replies (id TEXT PRIMARY KEY,guild TEXT,"
                "channel TEXT,recipient TEXT)"
            )

    @contextmanager
    def db(self):
        unlinked(self.root)
        for file in (self.path, self.path.with_name(self.path.name + "-journal")):
            unlinked(file)
            if file.exists() and (
                not file.is_file()
                or file.stat().st_nlink != 1
                or file.stat().st_size > 32 * 1024 * 1024
            ):
                raise ParliamentError("unsafe_ledger")
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def own_reply(self, event: Event, recipient: int) -> bool:
        with self.db() as db:
            return (
                db.execute(
                    "SELECT 1 FROM replies WHERE id=? AND guild=? AND channel=? AND recipient=?",
                    (str(event.reply_to), str(event.guild), str(event.channel), str(recipient)),
                ).fetchone()
                is not None
            )

    def accept(self, request: Request, policy: Policy) -> str | None:
        payload = json.dumps(asdict(request), sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        event = request.event
        identity = (
            f"bot:{event.author}:{request.correlation}"
            if event.author_bot
            else f"message:{event.message}"
        )
        key = hashlib.sha256(f"{event.guild}:{identity}:{policy.self_id}".encode()).hexdigest()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT digest FROM jobs WHERE id=?", (key,)).fetchone()
            if row:
                if row["digest"] != digest:
                    raise ParliamentError("duplicate_message_changed")
                return None
            if db.execute("SELECT count(*) FROM jobs").fetchone()[0] >= 10000:
                raise ParliamentError("ledger_full")
            if (
                db.execute(
                    "SELECT count(*) FROM jobs WHERE execution IN ('accepted','running')"
                ).fetchone()[0]
                >= 128
            ):
                raise ParliamentError("queue_full")
            if (
                event.author_bot
                and db.execute(
                    "SELECT 1 FROM jobs WHERE origin_bot=? AND (created>? OR execution IN "
                    "('accepted','running','unknown'))",
                    (str(event.author), time.time() - 120),
                ).fetchone()
            ):
                raise ParliamentError("bot_admission_budget_exhausted")
            db.execute(
                "INSERT INTO jobs(id,digest,policy,payload,created,origin_bot) VALUES(?,?,?,?,?,?)",
                (
                    key,
                    digest,
                    policy.digest,
                    payload,
                    time.time(),
                    str(event.author) if event.author_bot else None,
                ),
            )
        return key

    def claim(self, job: str, column: str, old: str, new: str) -> bool:
        if column not in {"execution", "delivery"}:
            raise ParliamentError("invalid_ledger_transition")
        with self.db() as db:
            return (
                db.execute(
                    f"UPDATE jobs SET {column}=? WHERE id=? AND {column}=?", (new, job, old)
                ).rowcount
                == 1
            )

    def get(self, job: str) -> dict:
        with self.db() as db:
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone())

    def finish(self, job: str, state: str, result: str):
        with self.db() as db:
            db.execute(
                "UPDATE jobs SET execution=?,result=? WHERE id=? AND execution='running'",
                (state, result, job),
            )

    def delivered(self, job: str, identifier: int, event: Event, self_id: int):
        with self.db() as db:
            db.execute("UPDATE jobs SET delivery='sent' WHERE id=? AND delivery='sending'", (job,))
            db.execute(
                "INSERT OR IGNORE INTO replies VALUES(?,?,?,?)",
                (str(identifier), str(event.guild), str(event.channel), str(self_id)),
            )

    def pending(self) -> list[str]:
        with self.db() as db:
            return [
                r[0]
                for r in db.execute(
                    "SELECT id FROM jobs WHERE execution='accepted' OR "
                    "(execution IN ('succeeded','failed') AND delivery='pending')"
                )
            ]


class Parliament:
    def __init__(self, policy: Policy, directory: Directory, backend: Backend):
        self.policy, self.directory, self.backend = policy, directory, backend
        self.ledger = Ledger(policy)
        self.execution_lock = asyncio.Lock()

    def fresh(self) -> Policy:
        policy = load_policy(self.policy.path)
        if policy is None or policy.digest != self.policy.digest:
            raise ParliamentError("policy_changed_or_disabled")
        return policy

    async def admit(self, event: Event) -> str | None:
        if event.webhook or event.system or event.everyone or event.author == self.policy.self_id:
            return None
        policy = self.fresh()
        own_reply = self.ledger.own_reply(event, policy.self_id)
        resolver = getattr(self.backend, "owns_reply", None)
        if (
            not own_reply
            and resolver is not None
            and event.reply_to is not None
            and not event.author_bot
            and event.guild == policy.guild
            and not event.content.startswith("<@")
        ):
            own_reply = await resolver(event, policy)
        request = parse(event, policy, own_reply=own_reply)
        if request is None:
            return None
        await authorize(request, policy, self.directory)
        return self.ledger.accept(request, policy)

    async def run(self, job: str, send) -> None:
        async with self.execution_lock:
            await self._run(job, send)

    async def _run(self, job: str, send) -> None:
        row = self.ledger.get(job)
        if row["policy"] != self.policy.digest:
            raise ParliamentError("admission_policy_changed")
        value = json.loads(row["payload"])
        request = Request(Event(**value.pop("event")), **value)
        if self.ledger.claim(job, "execution", "accepted", "running"):
            try:
                policy = self.fresh()
                await authorize(request, policy, self.directory)
                async with asyncio.timeout(120):
                    result = await self.backend.execute(request, policy, job)
                if not isinstance(result, str) or not result.strip() or len(result) > 6000:
                    raise ParliamentError("invalid_backend_result")
                self.ledger.finish(job, "succeeded", result)
            except ParliamentError as exc:
                self.ledger.finish(job, "failed", str(exc))
            except Exception:
                # External execution may have happened；never turn uncertainty into retry．
                self.ledger.finish(job, "unknown", "execution_unconfirmed")
                return
        row = self.ledger.get(job)
        if row["execution"] not in {"succeeded", "failed"}:
            return
        policy = self.fresh()
        await authorize(request, policy, self.directory, delivery=True)
        if self.ledger.claim(job, "delivery", "pending", "sending"):
            try:
                identifier = await send(request, job, row["result"])
                self.ledger.delivered(job, snowflake(identifier), request.event, policy.self_id)
            except Exception:
                # Sending is a permanent ambiguous state until operator reconciliation．
                raise ParliamentError("delivery_unconfirmed") from None

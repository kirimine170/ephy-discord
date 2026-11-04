"""Pending Discord approval queue. Only a reply is executed; other kinds stay stubs."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import timedelta

from ephy_discord.config import AppConfig
from ephy_discord.store import Store, format_time

REAL_KIND = "reply"
STUB_KIND = "stub"
NOT_APPROVER = "承認者だけが操作できます．"
NO_APPROVERS = "承認者が未設定です．"
EXPIRED = "この承認は期限切れです．"
ALREADY = "この承認はすでに処理されています．"
STUB_ACK = "承認を記録しました．この操作はまだ実行しません．"
REJECT_ACK = "却下しました．"
APPROVE_REPLY = "承認しました．返信を送ります．"


@dataclass(frozen=True)
class Decision:
    status: str
    kind: str
    text: str
    reply_text: str = ""
    action_id: str = ""


class ApprovalQueue:
    def __init__(self, config: AppConfig, store: Store) -> None:
        self.config = config
        self.store = store

    def propose(
        self,
        *,
        guild_id: int,
        channel_id: int,
        requester_user_id: int,
        kind: str,
        payload: dict,
    ) -> dict:
        if kind not in {REAL_KIND, STUB_KIND}:
            raise ValueError("unknown approval kind")
        if not self.config.approver_user_ids:
            raise ValueError("no approvers")
        now = self.store.clock.now()
        expires = now + timedelta(seconds=self.config.approval.expiry_seconds)
        action_id = uuid.uuid4().hex
        row = {
            "id": action_id,
            "created_at": format_time(now),
            "expires_at": format_time(expires),
            "guild_id": str(guild_id),
            "channel_id": str(channel_id),
            "requester_user_id": str(requester_user_id),
            "kind": kind,
            "payload_json": json.dumps(payload, ensure_ascii=False),
            "status": "pending",
        }
        self.store.insert_action(row)
        return row

    def pending(self) -> list[dict]:
        self.expire_due()
        return self.store.pending_actions()

    def expire_due(self) -> list[dict]:
        return self.store.expire_actions(format_time(self.store.clock.now()))

    def attach_message(self, action_id: str, message_id: str) -> None:
        self.store.save_action_status(
            action_id,
            status="pending",
            decided_by=None,
            decided_at=None,
            message_id=message_id,
        )

    def decide(self, action_id: str, user_id: int, decision: str) -> Decision:
        if decision not in {"approved", "rejected"}:
            raise ValueError("unknown decision")
        if user_id not in self.config.approver_user_ids:
            return Decision(status="forbidden", kind="", text=NOT_APPROVER, action_id=action_id)
        self.expire_due()
        action = self.store.get_action(action_id)
        if action is None:
            return Decision(status="missing", kind="", text=ALREADY, action_id=action_id)
        if action["status"] == "expired":
            return Decision(
                status="expired",
                kind=action["kind"],
                text=EXPIRED,
                action_id=action_id,
            )
        if action["status"] != "pending":
            return Decision(
                status=action["status"],
                kind=action["kind"],
                text=ALREADY,
                action_id=action_id,
            )
        now = format_time(self.store.clock.now())
        self.store.save_action_status(
            action_id,
            status=decision,
            decided_by=str(user_id),
            decided_at=now,
        )
        payload = json.loads(action["payload_json"])
        if decision == "rejected":
            return Decision(
                status="rejected",
                kind=action["kind"],
                text=REJECT_ACK,
                action_id=action_id,
            )
        if action["kind"] == REAL_KIND:
            return Decision(
                status="approved",
                kind=REAL_KIND,
                text=APPROVE_REPLY,
                reply_text=str(payload.get("text") or ""),
                action_id=action_id,
            )
        return Decision(
            status="approved",
            kind=STUB_KIND,
            text=STUB_ACK,
            action_id=action_id,
        )

    def mark_post_unknown(self, action_id: str) -> None:
        self.store.save_action_status(
            action_id,
            status="post_unknown",
            decided_by=None,
            decided_at=format_time(self.store.clock.now()),
        )

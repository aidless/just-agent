"""temporal_ledger.py — 可撤回的时间断言账本（FlowGrid vNext 设计参考）。

原始 messages 永远不覆盖、不删除；本账本只记录从消息中抽取出的时间断言的
assert / update / retract 事件。每次事件都会递增 timeline_revision，供检索缓存失效。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

Status = Literal["active", "superseded", "retracted"]
Operation = Literal["assert", "update", "retract"]


@dataclass(frozen=True)
class TemporalClaim:
    claim_id: str
    user_id: str
    session_id: str | None
    scope: str
    source_message_id: str
    event_epoch: float | None
    granularity: str
    confidence: str
    status: Status
    revision: int
    superseded_by: str | None
    reason: str | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TemporalLedger:
    """面向单个 SQLite 连接的追加式时间状态账本。

    scope 由上游解析器提供，例如 ``project:apple-q3:release_date``。
    没有高置信度 scope 时不要写账本：保留原始消息并让检索层按候选证据处理。
    """

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.con.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.con.executescript("""
        CREATE TABLE IF NOT EXISTS temporal_claims(
          claim_id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL,
          session_id TEXT,
          scope TEXT NOT NULL,
          source_message_id TEXT NOT NULL,
          event_epoch REAL,
          granularity TEXT NOT NULL,
          confidence TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('active','superseded','retracted')),
          revision INTEGER NOT NULL,
          superseded_by TEXT,
          reason TEXT,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_temporal_claim_active
          ON temporal_claims(user_id, scope, status, revision DESC);
        CREATE TABLE IF NOT EXISTS temporal_timeline_revision(
          user_id TEXT PRIMARY KEY,
          revision INTEGER NOT NULL
        );
        """)
        self.con.commit()

    def _next_revision(self, user_id: str) -> int:
        row = self.con.execute(
            "SELECT revision FROM temporal_timeline_revision WHERE user_id=?", (user_id,)
        ).fetchone()
        revision = int(row["revision"]) + 1 if row else 1
        self.con.execute(
            "INSERT INTO temporal_timeline_revision(user_id,revision) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET revision=excluded.revision",
            (user_id, revision),
        )
        return revision

    def apply(
        self,
        *,
        operation: Operation,
        user_id: str,
        session_id: str | None,
        scope: str,
        source_message_id: str,
        event_epoch: float | None = None,
        granularity: str = "unknown",
        confidence: str = "UNKNOWN",
        target_claim_id: str | None = None,
        reason: str | None = None,
        restore_previous_on_retract: bool = False,
    ) -> TemporalClaim | None:
        """追加一条时间治理事件并提交。

        update：旧 active 断言改为 superseded，新断言 active。
        retract：目标（或当前 active）改为 retracted；默认不自动恢复旧版本，
        因为“撤回新说法”不等价于“旧说法重新为真”。如业务明确需要，可设置
        restore_previous_on_retract=True，但必须在 UI 明示该策略。
        """
        if operation not in {"assert", "update", "retract"}:
            raise ValueError("invalid operation")
        if not user_id or not scope or not source_message_id:
            raise ValueError("user_id, scope and source_message_id are required")

        revision = self._next_revision(user_id)
        if operation == "retract":
            target = target_claim_id or self._active_id(user_id, scope)
            if not target:
                self.con.commit()
                return None
            self.con.execute(
                "UPDATE temporal_claims SET status='retracted', reason=?, revision=? WHERE claim_id=? AND user_id=?",
                (reason or "retracted_by_user", revision, target, user_id),
            )
            if restore_previous_on_retract:
                prior = self.con.execute(
                    "SELECT claim_id FROM temporal_claims WHERE user_id=? AND scope=? AND status='superseded' "
                    "ORDER BY revision DESC LIMIT 1",
                    (user_id, scope),
                ).fetchone()
                if prior:
                    self.con.execute(
                        "UPDATE temporal_claims SET status='active', reason='restored_after_retract', revision=? "
                        "WHERE claim_id=?",
                        (revision, prior["claim_id"]),
                    )
            self.con.commit()
            return self.get(target)

        claim_id = "tc_" + uuid.uuid4().hex[:16]
        if operation == "update":
            self.con.execute(
                "UPDATE temporal_claims SET status='superseded', superseded_by=?, revision=? "
                "WHERE user_id=? AND scope=? AND status='active'",
                (claim_id, revision, user_id, scope),
            )
        claim = TemporalClaim(
            claim_id=claim_id, user_id=user_id, session_id=session_id, scope=scope,
            source_message_id=source_message_id, event_epoch=event_epoch,
            granularity=granularity, confidence=confidence, status="active",
            revision=revision, superseded_by=None, reason=reason,
        )
        self.con.execute(
            "INSERT INTO temporal_claims VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (*claim.__dict__.values(), _now()),
        )
        self.con.commit()
        return claim

    def _active_id(self, user_id: str, scope: str) -> str | None:
        row = self.con.execute(
            "SELECT claim_id FROM temporal_claims WHERE user_id=? AND scope=? AND status='active' "
            "ORDER BY revision DESC LIMIT 1", (user_id, scope)
        ).fetchone()
        return row["claim_id"] if row else None

    def get(self, claim_id: str) -> TemporalClaim | None:
        row = self.con.execute("SELECT * FROM temporal_claims WHERE claim_id=?", (claim_id,)).fetchone()
        return TemporalClaim(**{k: row[k] for k in TemporalClaim.__dataclass_fields__}) if row else None

    def active(self, user_id: str, scope: str) -> TemporalClaim | None:
        cid = self._active_id(user_id, scope)
        return self.get(cid) if cid else None

    def cache_key(self, user_id: str, query: str) -> str:
        """检索缓存必须包含版本，任何更新/撤回都会自动失效旧结果。"""
        row = self.con.execute("SELECT revision FROM temporal_timeline_revision WHERE user_id=?", (user_id,)).fetchone()
        return f"temporal-v{row['revision'] if row else 0}:{user_id}:{query}"


if __name__ == "__main__":
    con = sqlite3.connect(":memory:")
    ledger = TemporalLedger(con)
    scope = "project:apple-q3:release_date"
    first = ledger.apply(operation="assert", user_id="u1", session_id="s1", scope=scope,
                         source_message_id="m1", event_epoch=1786665600.0, granularity="day", confidence="HIGH")
    second = ledger.apply(operation="update", user_id="u1", session_id="s1", scope=scope,
                          source_message_id="m2", event_epoch=1787875200.0, granularity="day", confidence="MEDIUM",
                          reason="user_changed_date")
    assert ledger.active("u1", scope).claim_id == second.claim_id
    ledger.apply(operation="retract", user_id="u1", session_id="s1", scope=scope,
                 source_message_id="m3", target_claim_id=second.claim_id, reason="user_retracted_update")
    assert ledger.active("u1", scope) is None
    print("temporal ledger lifecycle OK;", ledger.cache_key("u1", "发布日期是什么？"))

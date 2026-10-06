"""追加式事件存储与协同台账（SQLite）。

时间线只增不改：事件一经写入不可更新、不可删除。标识相同而内容变化的上报
作为争议副本单独保留，不覆盖原事件。服务责任槽位靠唯一约束保证并发下只有
一方认领成功。提醒按事件发生时推导出的原始到期时间落库，系统停机重启后
仍按原时间顺序补发。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS events (
        seq              INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id          TEXT NOT NULL,
        event_id         TEXT NOT NULL,
        event_type       TEXT NOT NULL,
        aggregate_type   TEXT NOT NULL,
        aggregate_id     TEXT NOT NULL,
        occurred_at      TEXT NOT NULL,
        version          INTEGER NOT NULL,
        payload          TEXT NOT NULL,
        content_hash     TEXT NOT NULL,
        conflict_of      TEXT,
        excluded         INTEGER NOT NULL DEFAULT 0,
        recorded_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_timeline ON events(case_id, occurred_at, seq)",
    # 仅对进入有效时间线的行约束事件标识唯一；被标记 excluded 的争议副本可同标识并存。
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_events_active_id
    ON events(case_id, event_id) WHERE excluded = 0
    """,
    """
    CREATE TABLE IF NOT EXISTS disputes (
        dispute_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id             TEXT NOT NULL,
        event_id            TEXT NOT NULL,
        original_seq        INTEGER NOT NULL,
        conflicting_seq     INTEGER NOT NULL,
        existing_hash       TEXT NOT NULL,
        conflicting_hash    TEXT NOT NULL,
        created_at          TEXT NOT NULL,
        resolved_at         TEXT,
        resolution          TEXT,
        UNIQUE(case_id, event_id, conflicting_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS service_slots (
        case_id        TEXT NOT NULL,
        slot_key       TEXT NOT NULL,
        claimed_by     TEXT NOT NULL,
        claim_event_id TEXT NOT NULL,
        claimed_at     TEXT NOT NULL,
        PRIMARY KEY(case_id, slot_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reminders (
        reminder_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id          TEXT NOT NULL,
        kind             TEXT NOT NULL,
        due_at           TEXT NOT NULL,
        ref_event_id     TEXT NOT NULL,
        ref_aggregate_id TEXT,
        status           TEXT NOT NULL DEFAULT 'due',
        sent_at          TEXT,
        created_at       TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_at)",
    """
    CREATE TABLE IF NOT EXISTS access_grants (
        staff_id    TEXT NOT NULL,
        case_id     TEXT NOT NULL,
        role_scope  TEXT NOT NULL,
        provider_id TEXT,
        PRIMARY KEY(staff_id, case_id)
    )
    """,
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class EventStore:
    """封装 SQLite 连接的追加式存储。"""

    def __init__(self, target: str | Path | sqlite3.Connection = ":memory:") -> None:
        if isinstance(target, sqlite3.Connection):
            self.conn = target
            self._owns_conn = False
        else:
            self.conn = sqlite3.connect(str(target), timeout=10)
            self._owns_conn = True
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA foreign_keys = ON")
        for statement in SCHEMA_STATEMENTS:
            self.conn.execute(statement)
        self.conn.commit()

    def close(self) -> None:
        if self._owns_conn:
            self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- 案例与访问 ----------------------------------------------------

    def grant_access(
        self, staff_id: str, case_id: str, role_scope: str, provider_id: str | None = None
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO access_grants(staff_id, case_id, role_scope, provider_id) "
            "VALUES (?, ?, ?, ?)",
            (staff_id, case_id, role_scope, provider_id),
        )
        self.conn.commit()

    def access_for(self, staff_id: str, case_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM access_grants WHERE staff_id = ? AND case_id = ?",
            (staff_id, case_id),
        ).fetchone()

    # ---- 事件追加 ------------------------------------------------------

    def find_event(self, case_id: str, event_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM events WHERE case_id = ? AND event_id = ? AND excluded = 0",
            (case_id, event_id),
        ).fetchone()

    def append_event(
        self,
        event: Mapping[str, Any],
        *,
        case_id: str,
        content_hash: str,
        recorded_at: str,
        conflict_of: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO events(case_id, event_id, event_type, aggregate_type, aggregate_id, "
            "occurred_at, version, payload, content_hash, conflict_of, excluded, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                case_id,
                event["event_id"],
                event["event_type"],
                event["aggregate_type"],
                event["aggregate_id"],
                event["occurred_at"],
                event["version"],
                json.dumps(event["payload"], ensure_ascii=False, sort_keys=True),
                content_hash,
                conflict_of,
                1 if conflict_of else 0,
                recorded_at,
            ),
        )
        return int(cur.lastrowid)

    def open_dispute(
        self,
        *,
        case_id: str,
        event_id: str,
        original_seq: int,
        conflicting_seq: int,
        existing_hash: str,
        conflicting_hash: str,
        created_at: str,
    ) -> int | None:
        """登记新争议；同一冲突内容重复上报时返回 None（争议本身也幂等）。"""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO disputes(case_id, event_id, original_seq, conflicting_seq, "
            "existing_hash, conflicting_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                case_id,
                event_id,
                original_seq,
                conflicting_seq,
                existing_hash,
                conflicting_hash,
                created_at,
            ),
        )
        return int(cur.lastrowid) if cur.rowcount else None

    def disputes(self, case_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM disputes WHERE case_id = ? ORDER BY dispute_id", (case_id,)
            )
        )

    def timeline(self, case_id: str, *, include_excluded: bool = False) -> list[sqlite3.Row]:
        """按真实发生时间（同刻按接收序号）返回事件，可回溯且稳定。"""
        sql = "SELECT * FROM events WHERE case_id = ?"
        if not include_excluded:
            sql += " AND excluded = 0"
        sql += " ORDER BY occurred_at, seq"
        return list(self.conn.execute(sql, (case_id,)))

    # ---- 服务责任槽位 --------------------------------------------------

    def claim_slot(
        self, *, case_id: str, slot_key: str, provider_id: str, event_id: str, now: str
    ) -> sqlite3.Row:
        """插入认领记录；槽位已被他人持有时抛出 sqlite3.IntegrityError。"""
        self.conn.execute(
            "INSERT INTO service_slots(case_id, slot_key, claimed_by, claim_event_id, claimed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (case_id, slot_key, provider_id, event_id, now),
        )
        return self.conn.execute(
            "SELECT * FROM service_slots WHERE case_id = ? AND slot_key = ?",
            (case_id, slot_key),
        ).fetchone()

    def slot_claim(self, case_id: str, slot_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM service_slots WHERE case_id = ? AND slot_key = ?",
            (case_id, slot_key),
        ).fetchone()

    # ---- 提醒 ----------------------------------------------------------

    def add_reminder(
        self,
        *,
        case_id: str,
        kind: str,
        due_at: str,
        ref_event_id: str,
        ref_aggregate_id: str | None,
        now: str,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO reminders(case_id, kind, due_at, ref_event_id, ref_aggregate_id, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, 'due', ?)",
            (case_id, kind, due_at, ref_event_id, ref_aggregate_id, now),
        )
        return int(cur.lastrowid)

    def close_reminders(self, case_id: str, kind: str, ref_event_id: str) -> int:
        cur = self.conn.execute(
            "UPDATE reminders SET status = 'closed' "
            "WHERE case_id = ? AND kind = ? AND ref_event_id = ? AND status = 'due'",
            (case_id, kind, ref_event_id),
        )
        return int(cur.rowcount)

    def due_reminders(self, now: str) -> list[sqlite3.Row]:
        """所有到期未发提醒，按原始到期时间排序——停机重启后顺序不变。"""
        return list(
            self.conn.execute(
                "SELECT * FROM reminders WHERE status = 'due' AND due_at <= ? ORDER BY due_at, reminder_id",
                (now,),
            )
        )

    def mark_reminder_sent(self, reminder_id: int, sent_at: str) -> None:
        self.conn.execute(
            "UPDATE reminders SET status = 'sent', sent_at = ? WHERE reminder_id = ?",
            (sent_at, reminder_id),
        )

    def overdue_open(self, now: str) -> list[sqlite3.Row]:
        """已逾期待办但授权/复核仍未补齐的风险提醒。"""
        return list(
            self.conn.execute(
                "SELECT * FROM reminders WHERE status = 'due' AND due_at < ? ORDER BY due_at, reminder_id",
                (now,),
            )
        )


def event_payload(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return json.loads(row["payload"])
    return dict(row["payload"]) if isinstance(row.get("payload"), str) else dict(row["payload"])


def rows_to_events(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "seq": row["seq"],
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "aggregate_type": row["aggregate_type"],
                "aggregate_id": row["aggregate_id"],
                "occurred_at": row["occurred_at"],
                "version": row["version"],
                "payload": json.loads(row["payload"]),
                "recorded_at": row["recorded_at"],
                "conflict_of": row["conflict_of"],
            }
        )
    return result

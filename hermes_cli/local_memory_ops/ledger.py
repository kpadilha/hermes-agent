"""Local belief/evidence ledger and deterministic memory write gate.

This module is deliberately self-hosted and profile-scoped. It does not replace
Honcho or the built-in prompt memory; it records a structured, auditable shadow
of durable memory writes so higher layers can reason about ADD/UPDATE/SUPERSEDE
/NOOP decisions instead of only appending prose.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home


_KRISHNA_RE = re.compile(r"\bkrishna\b", re.IGNORECASE)
_PREFERS_RE = re.compile(r"\bprefers?\b", re.IGNORECASE)
_EXPECTS_RE = re.compile(r"\bexpects?\b", re.IGNORECASE)


class BeliefLedger:
    """Small SQLite store for structured memory decisions and records."""

    def __init__(self, db_path: Optional[Path | str] = None) -> None:
        self.db_path = Path(db_path) if db_path else get_hermes_home() / "memory-ledger.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object TEXT NOT NULL,
                    source TEXT NOT NULL,
                    evidence_ref TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.7,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    valid_from REAL,
                    valid_until REAL,
                    supersedes TEXT,
                    contradicted_by TEXT,
                    storage_targets TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS memory_write_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    target TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    old_content TEXT NOT NULL DEFAULT '',
                    record_id INTEGER,
                    superseded_record_id INTEGER,
                    source TEXT NOT NULL DEFAULT '',
                    evidence_ref TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memory_records_status
                    ON memory_records(status);
                CREATE INDEX IF NOT EXISTS idx_memory_records_subject_predicate
                    ON memory_records(subject, predicate, status);
                CREATE INDEX IF NOT EXISTS idx_memory_records_object
                    ON memory_records(object);
                CREATE INDEX IF NOT EXISTS idx_memory_decisions_operation
                    ON memory_write_decisions(operation);
                """
            )

    def add_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        now = float(record.get("created_at") or time.time())
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO memory_records (
                    type, subject, predicate, object, source, evidence_ref,
                    confidence, status, created_at, last_seen_at, valid_from,
                    valid_until, supersedes, contradicted_by, storage_targets
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.get("type", "fact"),
                    record.get("subject", "unknown"),
                    record.get("predicate", "states"),
                    record.get("object", ""),
                    record.get("source", ""),
                    record.get("evidence_ref", ""),
                    float(record.get("confidence", 0.7)),
                    record.get("status", "active"),
                    now,
                    float(record.get("last_seen_at") or now),
                    record.get("valid_from"),
                    record.get("valid_until"),
                    record.get("supersedes", ""),
                    record.get("contradicted_by", ""),
                    record.get("storage_targets", ""),
                ),
            )
            row_id = cur.lastrowid
        return self.get_record(int(row_id))

    def get_record(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM memory_records WHERE id = ?", (record_id,)
            ).fetchone()
        return dict(row) if row else {}

    def find_exact_active(self, content: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM memory_records
                WHERE status = 'active' AND object = ?
                ORDER BY id DESC LIMIT 1
                """,
                (content.strip(),),
            ).fetchone()
        return dict(row) if row else None

    def find_active_subject_predicate(self, subject: str, predicate: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_records
                WHERE status = 'active' AND subject = ? AND predicate = ?
                ORDER BY id DESC
                """,
                (subject, predicate),
            ).fetchall()
        return [dict(row) for row in rows]

    def _require_row_updated(self, cursor: sqlite3.Cursor, *, operation: str, record_id: int) -> None:
        """Fail loudly when a ledger mutation affects no rows.

        SQLite treats UPDATE/DELETE with no matching row as success. For the
        memory ledger this is dangerous: callers can believe a record was
        updated/deleted while nothing changed. Mutating APIs must therefore
        verify rowcount and surface stale IDs/status mismatches as explicit
        failures.
        """
        if cursor.rowcount != 1:
            raise ValueError(f"ledger {operation} affected {cursor.rowcount} rows for record_id={record_id}")

    def mark_superseded(self, record_id: int, *, by_content: str = "") -> None:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE memory_records
                SET status = 'superseded', valid_until = ?, contradicted_by = ?
                WHERE id = ? AND status = 'active'
                """,
                (now, by_content, record_id),
            )
            self._require_row_updated(cur, operation="mark_superseded", record_id=record_id)

    def update_record(self, record_id: int, *, content: str, source: str, evidence_ref: str) -> Dict[str, Any]:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE memory_records
                SET object = ?, source = ?, evidence_ref = ?, last_seen_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (content.strip(), source, evidence_ref, now, record_id),
            )
            self._require_row_updated(cur, operation="update_record", record_id=record_id)
        return self.get_record(record_id)

    def mark_deleted(self, record_id: int, *, evidence_ref: str = "") -> Dict[str, Any]:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE memory_records
                SET status = 'deleted', valid_until = ?, contradicted_by = ?
                WHERE id = ? AND status = 'active'
                """,
                (now, evidence_ref, record_id),
            )
            self._require_row_updated(cur, operation="mark_deleted", record_id=record_id)
        return self.get_record(record_id)

    def touch_record(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE memory_records SET last_seen_at = ? WHERE id = ? AND status = 'active'",
                (time.time(), record_id),
            )
            self._require_row_updated(cur, operation="touch_record", record_id=record_id)
        return self.get_record(record_id)

    def record_decision(self, decision: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_write_decisions (
                    operation, reason, target, content, old_content, record_id,
                    superseded_record_id, source, evidence_ref, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.get("operation", ""),
                    decision.get("reason", ""),
                    decision.get("target", ""),
                    decision.get("content", ""),
                    decision.get("old_content", ""),
                    decision.get("record", {}).get("id"),
                    decision.get("superseded_record_id"),
                    decision.get("source", ""),
                    decision.get("evidence_ref", ""),
                    float(decision.get("created_at") or time.time()),
                ),
            )

    def search(self, query: str, *, limit: int = 20) -> List[Dict[str, Any]]:
        pattern = f"%{query}%"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_records
                WHERE object LIKE ? OR subject LIKE ? OR predicate LIKE ?
                ORDER BY id DESC LIMIT ?
                """,
                (pattern, pattern, pattern, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_records(self, *, status: str | None = None) -> List[Dict[str, Any]]:
        """Return all ledger records without a silent fixed cap.

        Projection/reconciliation paths must not call search("", limit=N) as a
        proxy for "all records". A fixed cap silently hides records once the
        ledger grows past that value, recreating the class of bug seen with
        paginated Honcho conclusions.
        """
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM memory_records WHERE status = ? ORDER BY id DESC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM memory_records ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def find_active_conflicts(self) -> Dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_records
                WHERE status = 'active'
                ORDER BY subject, predicate, id
                """
            ).fetchall()
        groups: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            groups.setdefault((item.get("subject", ""), item.get("predicate", "")), []).append(item)
        conflicts = []
        for (subject, predicate), records in groups.items():
            unique_objects = {r.get("object") for r in records}
            conflict_sensitive = any(
                r.get("type") in {"preference", "expectation", "belief"}
                or r.get("predicate") in {"prefers", "expects"}
                for r in records
            )
            if conflict_sensitive and len(records) > 1 and len(unique_objects) > 1:
                conflicts.append({
                    "subject": subject,
                    "predicate": predicate,
                    "records": records,
                })
        return {"conflict_count": len(conflicts), "conflicts": conflicts}

    def audit(self) -> Dict[str, Any]:
        with self._connect() as conn:
            record_rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM memory_records GROUP BY status"
            ).fetchall()
            decision_rows = conn.execute(
                "SELECT operation, COUNT(*) AS count FROM memory_write_decisions GROUP BY operation"
            ).fetchall()
            recent_rows = conn.execute(
                """
                SELECT operation, reason, target, content, record_id, created_at
                FROM memory_write_decisions
                ORDER BY id DESC LIMIT 10
                """
            ).fetchall()
        return {
            "db_path": str(self.db_path),
            "records": {row["status"]: row["count"] for row in record_rows},
            "decisions": {row["operation"]: row["count"] for row in decision_rows},
            "recent_decisions": [dict(row) for row in recent_rows],
        }


class MemoryWriteGate:
    """Deterministic local gate for durable memory writes."""

    def __init__(self, ledger: Optional[BeliefLedger] = None) -> None:
        self.ledger = ledger or BeliefLedger()

    def evaluate_and_record(
        self,
        *,
        target: str,
        content: str,
        source: str,
        evidence_ref: str,
        old_content: str = "",
    ) -> Dict[str, Any]:
        content = content.strip()
        old_content = (old_content or "").strip()
        now = time.time()
        exact = self.ledger.find_exact_active(content)
        if exact:
            record = self.ledger.touch_record(int(exact["id"]))
            decision = {
                "operation": "NOOP",
                "reason": "exact_duplicate",
                "target": target,
                "content": content,
                "old_content": old_content,
                "record": record,
                "source": source,
                "evidence_ref": evidence_ref,
                "created_at": now,
            }
            self.ledger.record_decision(decision)
            return decision

        record = self._build_record(
            target=target,
            content=content,
            source=source,
            evidence_ref=evidence_ref,
            now=now,
        )

        superseded: Optional[Dict[str, Any]] = None
        if old_content:
            superseded = self.ledger.find_exact_active(old_content)
        if superseded is None and record.get("type") in {"preference", "expectation", "belief"}:
            candidates = self.ledger.find_active_subject_predicate(
                record["subject"], record["predicate"]
            )
            superseded = next((c for c in candidates if c["object"] != content), None)

        operation = "ADD"
        if superseded:
            operation = "SUPERSEDE"
            self.ledger.mark_superseded(int(superseded["id"]), by_content=content)
            record["supersedes"] = str(superseded["id"])

        saved = self.ledger.add_record(record)
        decision = {
            "operation": operation,
            "reason": "new_record" if operation == "ADD" else "supersedes_prior_record",
            "target": target,
            "content": content,
            "old_content": old_content,
            "record": saved,
            "superseded_record_id": int(superseded["id"]) if superseded else None,
            "source": source,
            "evidence_ref": evidence_ref,
            "created_at": now,
        }
        self.ledger.record_decision(decision)
        return decision

    def update_record(
        self,
        *,
        record_id: int,
        content: str,
        source: str,
        evidence_ref: str,
    ) -> Dict[str, Any]:
        record = self.ledger.update_record(
            record_id,
            content=content,
            source=source,
            evidence_ref=evidence_ref,
        )
        decision = {
            "operation": "UPDATE",
            "reason": "record_updated",
            "target": record.get("storage_targets", ""),
            "content": content.strip(),
            "old_content": "",
            "record": record,
            "source": source,
            "evidence_ref": evidence_ref,
            "created_at": time.time(),
        }
        self.ledger.record_decision(decision)
        return decision

    def delete_record(
        self,
        *,
        record_id: int,
        source: str,
        evidence_ref: str,
    ) -> Dict[str, Any]:
        record = self.ledger.mark_deleted(record_id, evidence_ref=evidence_ref)
        decision = {
            "operation": "DELETE",
            "reason": "record_deleted",
            "target": record.get("storage_targets", ""),
            "content": record.get("object", ""),
            "old_content": record.get("object", ""),
            "record": record,
            "source": source,
            "evidence_ref": evidence_ref,
            "created_at": time.time(),
        }
        self.ledger.record_decision(decision)
        return decision

    def _build_record(
        self,
        *,
        target: str,
        content: str,
        source: str,
        evidence_ref: str,
        now: float,
    ) -> Dict[str, Any]:
        subject = "Krishna" if target == "user" or _KRISHNA_RE.search(content) else "system"
        lowered = content.lower()
        if _PREFERS_RE.search(content):
            record_type = "preference"
            predicate = "prefers"
        elif _EXPECTS_RE.search(content):
            record_type = "expectation"
            predicate = "expects"
        elif "uses" in lowered or "use " in lowered:
            record_type = "fact"
            predicate = "uses"
        else:
            record_type = "fact" if target == "memory" else "observation"
            predicate = "states"
        return {
            "type": record_type,
            "subject": subject,
            "predicate": predicate,
            "object": content,
            "source": source,
            "evidence_ref": evidence_ref,
            "confidence": 0.8 if record_type in {"preference", "expectation"} else 0.7,
            "status": "active",
            "created_at": now,
            "last_seen_at": now,
            "valid_from": now,
            "valid_until": None,
            "supersedes": "",
            "contradicted_by": "",
            "storage_targets": target,
        }

"""Profile-local durable audit ledger for cron execution attempts.

The ledger records what is known about each attempt; it is not a retry queue. Interrupted attempts
become ``unknown`` only after their owner process is proved gone — a start-time reading that fails
to match the claim-time fingerprint is not proof of death. Terminal states are immutable.
"""

from __future__ import annotations

import math
import os
import random
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now
from cron.constants import CLAIM_TTL_INACTIVITY_HEADROOM

# Optional test override. Production resolves the path at transaction time so dashboard operations
# that temporarily enter another profile cannot leak that profile's records into the import-time
# home.
EXECUTIONS_FILE: Optional[Path] = None
MAX_TERMINAL_EXECUTIONS = 1000
HANDOFF_ADOPTION_GRACE_SECONDS = 30.0
# Floor for the live-owner stale-claim bound (#115692); see _live_owner_stale_after_seconds.
LIVE_OWNER_STALE_CLAIM_FLOOR_SECONDS = 7200.0
_TERMINAL_STATES = ("completed", "failed", "unknown")
_lock = threading.RLock()
_db_init_lock = threading.Lock()
_initialized_files: Dict[Path, tuple[int, int, int]] = {}
_write_state = threading.local()
_PROCESS_ID = uuid.uuid4().hex
_WRITE_RETRY_SECONDS = 15.0
_READ_OPEN_RETRY_SECONDS = 30.0
_WRITE_RETRY_MIN_SECONDS = 0.01
_WRITE_RETRY_MAX_SECONDS = 0.25
_DELETE_WRITE_YIELD_SECONDS = 0.02
_REQUIRED_COLUMNS = frozenset({
    "id", "job_id", "source", "process_id", "pid", "process_started_at", "status",
    "handoff_pending", "handoff_started_at", "claimed_at", "started_at", "finished_at",
    "error", "delivery_outcome", "scheduled_instant",
})
_REQUIRED_INDEXES = frozenset({
    "idx_executions_job_claimed",
    "idx_executions_status_claimed",
    "idx_executions_occurrence",
})


# --- executions ledger --------------------------------------------------------------------------

def _db_path() -> Path:
    return Path(EXECUTIONS_FILE or (get_hermes_home().resolve() / "cron" / "executions.db"))


def _connection_fingerprint(
    path: Path, conn: sqlite3.Connection
) -> tuple[int, int, int]:
    stat = path.stat()
    schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    return stat.st_dev, stat.st_ino, schema_version


def _open_ledger_db(
    path: Path,
    initialized_files: Dict[Path, tuple[int, int, int]],
    init_lock: threading.Lock,
    initialize: Callable[[sqlite3.Connection], None],
    schema_is_current: Callable[[sqlite3.Connection], bool],
) -> sqlite3.Connection:
    from hermes_cli.sqlite_util import open_db
    from hermes_state_wal import apply_wal_with_fallback

    conn: Optional[sqlite3.Connection] = open_db(
        path,
        db_label="cron/executions.db",
        busy_timeout_ms=50,
        wal=False,
        wal_companions=True,
        synchronous_full=True,
    )
    try:
        with init_lock:
            fingerprint = _connection_fingerprint(path, conn)
            if initialized_files.get(path) != fingerprint:
                apply_wal_with_fallback(conn, db_label="cron/executions.db")
                if not schema_is_current(conn):
                    initialize(conn)
                initialized_files[path] = _connection_fingerprint(path, conn)
        opened, conn = conn, None
        return opened
    finally:
        if conn is not None:
            conn.close()


def _connect() -> sqlite3.Connection:
    # Late imports: a scheduler daemon that outlives an on-disk upgrade already has the OLD
    # ``hermes_cli.sqlite_util`` / ``cron.jobs`` cached, so new names must be resolved at call time,
    # not at import time (the guarantee cron/ledger.py used to carry, see e24c8499).
    from cron.jobs import _ensure_cron_dir

    path = _db_path()
    _ensure_cron_dir(path.parent)
    return _open_ledger_db(
        path, _initialized_files, _db_init_lock, _initialize_schema, _schema_is_current
    )


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_cli.sqlite_util import add_column_if_missing

    conn.execute(
        """CREATE TABLE IF NOT EXISTS executions (
             id TEXT PRIMARY KEY,
             job_id TEXT NOT NULL,
             source TEXT NOT NULL,
             process_id TEXT NOT NULL,
             pid INTEGER NOT NULL,
             process_started_at INTEGER,
             status TEXT NOT NULL CHECK(status IN
               ('claimed','running','completed','failed','unknown')),
             handoff_pending INTEGER NOT NULL DEFAULT 0,
             handoff_started_at REAL,
             claimed_at TEXT NOT NULL,
             started_at TEXT,
             finished_at TEXT,
             error TEXT
           )"""
    )
    add_column_if_missing(
        conn, "executions", "handoff_pending",
        "handoff_pending INTEGER NOT NULL DEFAULT 0",
    )
    add_column_if_missing(
        conn, "executions", "handoff_started_at", "handoff_started_at REAL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_job_claimed "
        "ON executions(job_id, claimed_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_status_claimed "
        "ON executions(status, claimed_at DESC, id DESC)"
    )
    add_column_if_missing(conn, "executions", "delivery_outcome", "delivery_outcome TEXT")
    add_column_if_missing(conn, "executions", "scheduled_instant", "scheduled_instant TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_occurrence "
        "ON executions(job_id, scheduled_instant) WHERE status='completed'"
    )


def _schema_is_current(conn: sqlite3.Connection) -> bool:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(executions)")}
    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='executions'"
        )
    }
    return _REQUIRED_COLUMNS <= columns and _REQUIRED_INDEXES <= indexes


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    from hermes_cli.sqlite_util import transaction

    conn = _open_with_retry(_connect, timeout=_READ_OPEN_RETRY_SECONDS)
    with _lock, transaction(conn) as conn:
        yield conn


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    return str(exc).strip().lower() in {
        "database is locked",
        "database table is locked",
        "database schema is locked",
    }


def _retry_delay(attempt: int) -> float:
    ceiling = min(_WRITE_RETRY_MAX_SECONDS, _WRITE_RETRY_MIN_SECONDS * (2**attempt))
    return random.uniform(_WRITE_RETRY_MIN_SECONDS, ceiling)


def _open_with_retry(
    connect: Callable[[], sqlite3.Connection], *, timeout: float = _WRITE_RETRY_SECONDS
) -> sqlite3.Connection:
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        try:
            return connect()
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(_retry_delay(attempt))
            attempt += 1


@contextmanager
def _serialized_write(
    connect: Callable[[], sqlite3.Connection], lock: threading.RLock, db_path: Path
) -> Iterator[sqlite3.Connection]:
    """Acquire SQLite's write reservation before blocking this process's readers.

    ``BEGIN IMMEDIATE`` puts contention at the only safe retry boundary: the body has not run yet.
    SQLite remains the cross-process arbiter; jitter prevents synchronized workers from repeatedly
    colliding after the busy timeout. Reentrant writes use a savepoint on the owning connection.
    """
    nested_conn = getattr(_write_state, "conn", None)
    if nested_conn is not None:
        if getattr(_write_state, "db_path", None) != db_path:
            raise RuntimeError("nested cron ledger writes cannot switch profile databases")
        depth = int(getattr(_write_state, "depth", 1)) + 1
        savepoint = f"cron_nested_{depth}"
        with lock:
            nested_conn.execute(f"SAVEPOINT {savepoint}")
            _write_state.depth = depth
            try:
                yield nested_conn
            except BaseException:
                with suppress(sqlite3.Error):
                    nested_conn.execute(f"ROLLBACK TO {savepoint}")
                with suppress(sqlite3.Error):
                    nested_conn.execute(f"RELEASE {savepoint}")
                raise
            else:
                nested_conn.execute(f"RELEASE {savepoint}")
            finally:
                _write_state.depth = depth - 1
        return

    deadline = time.monotonic() + _WRITE_RETRY_SECONDS
    attempt = 0
    conn: Optional[sqlite3.Connection] = None
    delete_mode = False
    while True:
        try:
            conn = connect()
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            delete_mode = bool(mode and str(mode[0]).lower() == "delete")
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.close()
                conn = None
            if not _is_busy(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(_retry_delay(attempt))
            attempt += 1

    assert conn is not None
    _write_state.conn = conn
    _write_state.db_path = db_path
    _write_state.depth = 1
    try:
        with lock:
            try:
                yield conn
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
            else:
                commit_attempt = 0
                while True:
                    try:
                        conn.execute("COMMIT")
                        break
                    except sqlite3.OperationalError as exc:
                        if not _is_busy(exc) or time.monotonic() >= deadline:
                            raise
                        time.sleep(_retry_delay(commit_attempt))
                        commit_attempt += 1
    finally:
        _write_state.conn = None
        _write_state.db_path = None
        _write_state.depth = 0
        conn.close()
        if delete_mode:
            # ponytail: DELETE has no fair writer queue; yield with no connection open so a
            # hot worker cannot reacquire forever. Remove when the SQLite fallback does.
            time.sleep(_DELETE_WRITE_YIELD_SECONDS)


@contextmanager
def _write_transaction() -> Iterator[sqlite3.Connection]:
    with _serialized_write(_connect, _lock, _db_path()) as conn:
        yield conn


def _fetch(conn: sqlite3.Connection, execution_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
    return dict(row) if row is not None else None


def _emit_execution_state(
    record: Optional[Dict[str, Any]], *, delivery_outcome: Optional[str] = None
) -> None:
    """Project durable state to monitoring without affecting ledger behavior."""
    try:
        from agent.monitoring.cron_health import emit_execution_state

        emit_execution_state(record, delivery_outcome=delivery_outcome)
    except Exception:
        pass


def _process_start_time(pid: int) -> Optional[int]:
    try:
        from gateway.status import get_process_start_time
        return get_process_start_time(pid)
    except Exception:
        return None


def _owner_is_live(pid: int, started_at: Optional[int]) -> bool:
    try:
        from gateway.status import _pid_exists
        if not _pid_exists(pid):
            return False
    except Exception:
        return True  # fail safe: inability to prove death must not rewrite state
    if started_at is None:
        return pid == os.getpid()
    current = _process_start_time(pid)
    if current is None:
        return True  # cannot compare -> cannot prove death; a misread must not rewrite state
    # Drifted same-host readings (#117505) are not proof of death; a live misread is still
    # bounded by the stale-claim sweep below.
    from gateway.status import start_time_fingerprints_match
    return start_time_fingerprints_match(started_at, current)


def _live_owner_stale_after_seconds() -> Optional[float]:
    """Age past which a claimed/running row with a LIVE owner is treated as wedged.

    Derived from the existing knobs, never a bare wall-clock constant:
    ``max(3 × HERMES_CRON_TIMEOUT, cron script timeout, 7200)``. Returns ``None`` (never reclaim
    live owners — today's behaviour) when the inactivity timeout is 0/unlimited or not a finite
    positive number: with no bound to derive from, fail closed.
    """
    from cron.scheduler import _cron_inactivity_seconds
    from cron.scheduler_script import _get_script_timeout

    inactivity = float(_cron_inactivity_seconds())
    if not math.isfinite(inactivity) or inactivity <= 0:
        return None
    return max(
        inactivity * CLAIM_TTL_INACTIVITY_HEADROOM,
        float(_get_script_timeout()),
        LIVE_OWNER_STALE_CLAIM_FLOOR_SECONDS,
    )


def _claim_age_seconds(claimed_at: str) -> float:
    """Seconds since ``claimed_at`` (NOT NULL, always the aware ISO string from hermes_time.now)."""
    return (_hermes_now() - datetime.fromisoformat(claimed_at)).total_seconds()


def _prune_unlocked(conn: sqlite3.Connection) -> None:
    conn.execute(
        """DELETE FROM executions WHERE id IN (
             SELECT id FROM executions
             WHERE status IN ('completed','failed','unknown')
             ORDER BY finished_at DESC, claimed_at DESC, id DESC LIMIT -1 OFFSET ?
           )""",
        (max(0, int(MAX_TERMINAL_EXECUTIONS)),),
    )


def create_execution(
    job_id: str, *, source: str, scheduled_instant: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a claimed attempt before executor/provider dispatch."""
    from cron.occurrences import scheduled_instant as canonical_instant

    now = _hermes_now().isoformat()
    execution_id = uuid.uuid4().hex
    pid = os.getpid()
    with _write_transaction() as conn:
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at,
                status, claimed_at, scheduled_instant)
               VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?, ?)""",
            (execution_id, str(job_id), str(source), _PROCESS_ID, pid,
             _process_start_time(pid), now, canonical_instant(scheduled_instant)),
        )
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record  # type: ignore[return-value]


def set_execution_occurrence(execution_id: str, instant: Optional[str]) -> None:
    """Bind the store-claimed snapshot before a provider hands it to a worker."""
    from cron.occurrences import scheduled_instant

    with _write_transaction() as conn:
        cur = conn.execute(
            "UPDATE executions SET scheduled_instant=? WHERE id=? AND status='claimed' "
            "AND handoff_pending=0 AND process_id=? AND pid=?",
            (scheduled_instant(instant), execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Cron occurrence could not be bound before dispatch")


def mark_execution_handoff_pending(execution_id: str) -> Optional[Dict[str, Any]]:
    """Fence restart recovery while an external worker is adopting a claim."""
    with _write_transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET handoff_pending=1, handoff_started_at=?
               WHERE id=? AND status='claimed'
                 AND process_id=? AND pid=?""",
            (time.time(), execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            return None
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record


def adopt_claimed_execution(execution_id: str) -> Optional[Dict[str, Any]]:
    """Atomically transfer and start an attempt in its worker process.

    The dispatching gateway creates the row before spawning a restart-safe
    worker.  Adoption is the single ``claimed`` → ``running`` gate: only the
    winner may acknowledge ownership or run side effects.
    """
    pid = os.getpid()
    process_started_at = _process_start_time(pid)
    now = _hermes_now().isoformat()
    with _write_transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET process_id=?, pid=?, process_started_at=?,
                   status='running', started_at=?, handoff_pending=0,
                   handoff_started_at=NULL
               WHERE id=? AND status='claimed' AND handoff_pending=1""",
            (_PROCESS_ID, pid, process_started_at, now, execution_id),
        )
        if cur.rowcount != 1:
            return None
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record


def mark_execution_running(execution_id: str) -> Optional[Dict[str, Any]]:
    """Transition one claimed attempt to running exactly once."""
    now = _hermes_now().isoformat()
    with _write_transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET status='running', started_at=?, handoff_pending=0,
                   handoff_started_at=NULL
               WHERE id=? AND status='claimed' AND handoff_pending=0
                 AND process_id=? AND pid=?""",
            (now, execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            return None
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record


def finish_execution(
    execution_id: str, *, success: bool, error: Optional[str] = None,
    delivery_outcome: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Write a terminal result once; terminal attempts cannot be rewritten."""
    now = _hermes_now().isoformat()
    status = "completed" if success else "failed"
    detail = None if success else (str(error) if error else "unknown failure")
    with _write_transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET status=?, finished_at=?, error=?, handoff_pending=0,
                   handoff_started_at=NULL, delivery_outcome=?
               WHERE id=? AND status IN ('claimed','running')
                 AND process_id=? AND pid=?""",
            (status, now, detail, delivery_outcome, execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            return None
        _prune_unlocked(conn)
        record = _fetch(conn, execution_id)
    _emit_execution_state(record, delivery_outcome=delivery_outcome)
    return record


_OWNER_GONE_REASON = (
    "Scheduler restarted after this execution's owner exited before a durable "
    "terminal state; whether side effects ran is unknown."
)
_OWNER_WEDGED_REASON = (
    "Owner process is still alive but the claim outlived the derived stale bound; "
    "treated as wedged (#115692). The process was not terminated; whether side effects "
    "ran is unknown."
)


def recover_interrupted_executions() -> int:
    """Mark abandoned attempts unknown without scheduling retries: rows whose owner is provably
    dead, plus rows whose live owner holds a claim older than the derived stale bound (the
    process is not killed)."""
    now = _hermes_now().isoformat()
    changed = 0
    recovered: List[Dict[str, Any]] = []
    # Derived on the first live-owned row only: the bound reads config, and the idle gateway
    # tick must stay config-free (tests/cron/test_idle_tick_config_skip.py).
    stale_after: Optional[float] = None
    stale_after_resolved = False
    with _write_transaction() as conn:
        rows = conn.execute(
            """SELECT id, status, process_id, pid, process_started_at,
                      handoff_pending, handoff_started_at, claimed_at
               FROM executions
               WHERE status IN ('claimed','running')"""
        ).fetchall()
        for row in rows:
            if row["process_id"] == _PROCESS_ID:
                continue
            reason = _OWNER_GONE_REASON
            if _owner_is_live(int(row["pid"]), row["process_started_at"]):
                # A live owner is normally a legitimately running job. A worker permanently
                # deadlocked (e.g. futex_wait behind a route/proxy flip, #115692) also passes
                # this check, so a claim older than the derived bound is treated as wedged
                # and released — the external-worker wait loop polls this ledger for a
                # terminal status, so the job can fire again. The wedged worker PROCESS is
                # NOT terminated here (leaked until host restart); rows owned by this process
                # (process_id == _PROCESS_ID, in-process runs) are skipped above and remain
                # out of scope.
                if not stale_after_resolved:
                    stale_after = _live_owner_stale_after_seconds()
                    stale_after_resolved = True
                if stale_after is None or _claim_age_seconds(row["claimed_at"]) <= stale_after:
                    continue
                reason = _OWNER_WEDGED_REASON
            handoff_started_at = row["handoff_started_at"]
            if (
                row["handoff_pending"]
                and handoff_started_at is not None
                and time.time() - float(handoff_started_at)
                < HANDOFF_ADOPTION_GRACE_SECONDS
            ):
                continue
            cur = conn.execute(
                """UPDATE executions
                   SET status='unknown', finished_at=?, error=?,
                       handoff_pending=0, handoff_started_at=NULL
                   WHERE id=? AND status=? AND process_id=? AND pid=?
                     AND handoff_pending=?
                     AND handoff_started_at IS ?""",
                (now, reason, row["id"], row["status"], row["process_id"], row["pid"],
                 row["handoff_pending"], row["handoff_started_at"]),
            )
            changed += cur.rowcount
            if cur.rowcount:
                record = _fetch(conn, row["id"])
                if record is not None:
                    recovered.append(record)
        if changed:
            _prune_unlocked(conn)
    for record in recovered:
        _emit_execution_state(record)
    return changed


def list_executions(
    *, job_id: Optional[str] = None, limit: int = 50, before_claimed_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return indexed, newest-first execution history with cursor pagination."""
    clauses: List[str] = []
    params: List[Any] = []
    if job_id is not None:
        clauses.append("job_id=?")
        params.append(str(job_id))
    if before_claimed_at is not None:
        clauses.append("claimed_at < ?")
        params.append(str(before_claimed_at))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM executions" + where
            + " ORDER BY claimed_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_execution(execution_id: str) -> Optional[Dict[str, Any]]:
    """Return one exact execution attempt, or ``None`` when it is absent."""
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM executions WHERE id=?",
            (str(execution_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def latest_execution(job_id: str) -> Optional[Dict[str, Any]]:
    rows = list_executions(job_id=job_id, limit=1)
    return rows[0] if rows else None


def latest_executions(job_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Load latest execution for many jobs in one indexed query."""
    clean = [str(job_id) for job_id in dict.fromkeys(job_ids) if job_id]
    if not clean:
        return {}
    placeholders = ",".join("?" for _ in clean)
    with _transaction() as conn:
        rows = conn.execute(
            f"""SELECT e.* FROM executions e
                WHERE e.job_id IN ({placeholders})
                  AND e.id=(SELECT e2.id FROM executions e2
                            WHERE e2.job_id=e.job_id
                            ORDER BY e2.claimed_at DESC, e2.id DESC LIMIT 1)""",
            clean,
        ).fetchall()
    return {row["job_id"]: dict(row) for row in rows}

"""Durable cron execution-ledger behavior."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import cast

import pytest


def _point_ledger(monkeypatch, tmp_path):
    from cron import executions

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return executions


class _FaultConnection:
    """Inject a failed SQLite operation before delegating to a disposable real database."""

    def __init__(self, real, fault):
        self.real = real
        self.fault = fault
        self.closed = False
        self.successful = []

    def execute(self, sql, params=()):
        operation = sql.strip().split()[0].upper()
        if self.fault(operation):
            error = sqlite3.OperationalError("database is locked")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error
        result = self.real.execute(sql, params)
        self.successful.append(operation)
        return result

    @property
    def in_transaction(self):
        return self.real.in_transaction

    def __enter__(self):
        self.real.__enter__()
        return self

    def __exit__(self, *args):
        return self.real.__exit__(*args)

    def close(self):
        self.closed = True
        self.real.close()


def _retry_writer_case(executions, monkeypatch, writer):
    monkeypatch.setattr(executions, "_process_start_time", lambda _pid: 123)
    row = executions.create_execution("retry-job", source="builtin")
    execution_id = row["id"]
    calls = {
        "progress": lambda: executions.touch_execution_progress(execution_id),
        "missed": lambda: executions.record_missed_occurrences(
            "retry-job", count=3, first_instant=None, detail="offline"),
        "recovery": lambda: executions.recover_receipted_execution(
            execution_id, "retry-job", success=True, delivery_outcome="delivered"),
        "delivery": lambda: executions.settle_delivery_outcome(execution_id, "delivered"),
        "dead_owner": lambda: executions.terminalize_dead_owner(execution_id, reason="exit 9"),
        "give_up": lambda: executions.give_up_execution(execution_id, reason="unverified"),
    }
    if writer == "delivery":
        executions.finish_execution(execution_id, success=True, delivery_outcome="queued")
    if writer in {"recovery", "dead_owner"}:
        monkeypatch.setattr(executions, "_PROCESS_ID", "replacement")
        monkeypatch.setattr(executions, "_owner_is_live", lambda *_args: False)
    return execution_id, calls[writer]


@pytest.mark.parametrize("writer", ["progress", "missed", "recovery", "delivery", "dead_owner", "give_up"])
@pytest.mark.parametrize("seam", ["BEGIN", "UPDATE", "COMMIT"])
def test_all_ledger_writers_retry_without_replaying_effects(monkeypatch, tmp_path, writer, seam):
    executions = _point_ledger(monkeypatch, tmp_path)
    _execution_id, write = _retry_writer_case(executions, monkeypatch, writer)
    operation = "INSERT" if seam == "UPDATE" and writer == "missed" else seam
    failures = []
    connections = []
    emissions = []
    metrics = []
    real_connect = executions._connect

    def fault(op):
        if op == operation and not failures:
            failures.append(op)
            return True
        return False

    def connect():
        conn = _FaultConnection(real_connect(), fault)
        connections.append(conn)
        return conn

    monkeypatch.setattr(executions, "_connect", connect)
    monkeypatch.setattr(executions, "_emit_execution_state", lambda *a, **k: emissions.append(a))
    monkeypatch.setattr(executions, "record_cron_finish", lambda *a: metrics.append(a))
    assert write()
    assert failures == [operation], "writer bypassed the protected transaction seam"
    assert all(conn.closed for conn in connections)
    assert sum(conn.successful.count(operation) for conn in connections) == 1
    assert len(emissions) == (0 if writer in {"progress", "delivery"} else 1)
    assert len(metrics) == (1 if writer == "recovery" else 0)
    with sqlite3.connect(str(executions.EXECUTIONS_FILE)) as conn:
        rows = conn.execute("SELECT status, delivery_outcome, progress_at FROM executions WHERE job_id='retry-job'").fetchall()
    assert len(rows) == (2 if writer == "missed" else 1)
    if writer == "progress":
        assert rows[0][2]
    elif writer == "delivery":
        assert rows[0][:2] == ("completed", "delivered")
        assert write() is False
    elif writer == "recovery":
        assert rows[0][:2] == ("completed", "delivered")
        assert write() is None
    elif writer in {"give_up", "dead_owner"}:
        assert rows[0][0] == "unknown"
        assert write() is False
    else:
        assert sorted(row[0] for row in rows) == ["claimed", "missed"]
    assert getattr(executions._write_state, "conn", None) is None


@pytest.mark.parametrize("seam", ["BEGIN", "UPDATE", "SELECT", "DELETE", "COMMIT"])
@pytest.mark.parametrize("busy", [True, False])
def test_ledger_retry_failures_roll_back_and_close(monkeypatch, tmp_path, seam, busy):
    executions = _point_ledger(monkeypatch, tmp_path)
    execution_id, write = _retry_writer_case(executions, monkeypatch, "give_up")
    before = executions.get_execution(execution_id)
    clock = [0.0]
    calls = []
    connections = []
    emitted = []
    error = sqlite3.OperationalError("database is locked" if busy else "disk I/O error")
    real_connect = executions._connect

    def fault(op):
        if op == seam:
            calls.append(op)
            clock[0] += 8.0
            raise error
        return False

    def connect():
        conn = _FaultConnection(real_connect(), fault)
        connections.append(conn)
        return conn

    monkeypatch.setattr(executions, "_connect", connect)
    monkeypatch.setattr(executions, "_emit_execution_state", lambda *a, **k: emitted.append(a))
    monkeypatch.setattr(executions.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(executions.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    with pytest.raises(sqlite3.OperationalError) as caught:
        write()
    assert caught.value is error
    assert len(calls) == (2 if busy else 1)
    assert all(conn.closed for conn in connections)
    assert emitted == []
    assert executions._write_state.conn is None
    assert getattr(executions._write_state, "deadline", None) is None
    with sqlite3.connect(str(executions.EXECUTIONS_FILE)) as conn:
        conn.row_factory = sqlite3.Row
        assert dict(conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()) == before
    monkeypatch.setattr(executions, "_connect", real_connect)
    assert write() is True  # Failure has not leaked ownership or a partial terminal state.


def test_statement_and_commit_share_acquisition_budget(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    execution_id, write = _retry_writer_case(executions, monkeypatch, "give_up")
    clock = [0.0]
    attempts = []
    real_connect = executions._connect

    def fault(op):
        if op in {"BEGIN", "UPDATE", "COMMIT"}:
            attempts.append(op)
            if attempts.count(op) == 1:
                clock[0] += 6.0
                return True
        return False

    monkeypatch.setattr(executions, "_connect", lambda: _FaultConnection(real_connect(), fault))
    monkeypatch.setattr(executions.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(executions.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        write()
    assert attempts == ["BEGIN", "BEGIN", "UPDATE", "UPDATE", "COMMIT"]
    monkeypatch.setattr(executions, "_connect", real_connect)
    assert executions.get_execution(execution_id)["status"] == "claimed"


def test_nested_ledger_writes_keep_savepoints_and_profile_isolation(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    execution_id, write = _retry_writer_case(executions, monkeypatch, "give_up")
    with executions._write_transaction() as outer:
        deadline = executions._write_state.deadline
        with pytest.raises(ValueError, match="abort inner"):
            with executions._write_transaction() as inner:
                assert inner is outer
                assert write() is True
                assert executions._write_state.deadline == deadline
                raise ValueError("abort inner")
        assert executions._write_state.depth == 1
        assert outer.execute("SELECT status FROM executions WHERE id=?", (execution_id,)).fetchone()[0] == "claimed"
        with monkeypatch.context() as scope:
            scope.setattr(executions, "EXECUTIONS_FILE", tmp_path / "foreign.db")
            with pytest.raises(RuntimeError, match="switch profile"):
                write()
        assert not (tmp_path / "foreign.db").exists()
        assert write() is True
    assert executions.get_execution(execution_id)["status"] == "unknown"
    assert executions._write_state.conn is None
    assert executions._write_state.deadline is None


@pytest.mark.parametrize("seam", ["BEGIN", "INSERT", "SELECT", "UPDATE", "COMMIT"])
def test_shared_incident_writer_retries_without_duplicate_rows(monkeypatch, tmp_path, seam):
    from cron import incidents

    executions = _point_ledger(monkeypatch, tmp_path)
    executions.create_execution("seed", source="builtin")
    incidents.list_incidents()  # Initialise before injecting transaction faults.
    real_connect = incidents._connect
    failures = []

    def fault(op):
        if op == seam and not failures:
            failures.append(op)
            return True
        return False

    monkeypatch.setattr(incidents, "_connect", lambda: _FaultConnection(real_connect(), fault))
    incident_id, is_new = incidents.upsert_incident("retry-job", "offline failure")
    assert is_new is True
    assert incidents.upsert_incident("retry-job", "offline failure") == (incident_id, False)
    assert incidents.set_incident_state(incident_id, "alerted") is True
    assert incidents.close_incidents_for_recovered_job("retry-job") == 1
    assert failures == [seam]
    assert len(incidents.list_incidents()) == 1
    assert incidents.get_incident(incident_id)["state"] == "resolved"
    assert executions._write_state.conn is None


@pytest.mark.parametrize("invalidated", ["snapshot", "auto_rollback"])
def test_statement_retry_refuses_invalidated_transactions(monkeypatch, tmp_path, invalidated):
    executions = _point_ledger(monkeypatch, tmp_path)
    execution_id, write = _retry_writer_case(executions, monkeypatch, "give_up")
    real_connect = executions._connect
    attempts = []
    real = real_connect()
    error = sqlite3.OperationalError("database is locked")
    if invalidated == "snapshot":
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY_SNAPSHOT

    def fault(op):
        if op == "UPDATE":
            attempts.append(op)
            if invalidated == "auto_rollback":
                real.rollback()
            raise error
        return False

    monkeypatch.setattr(executions, "_connect", lambda: _FaultConnection(real, fault))
    with pytest.raises(sqlite3.OperationalError) as caught:
        write()
    assert caught.value is error
    assert attempts == ["UPDATE"]
    monkeypatch.setattr(executions, "_connect", real_connect)
    assert executions.get_execution(execution_id)["status"] == "claimed"


def test_schema_cache_invalidates_after_serial_replacement_and_schema_change(monkeypatch, tmp_path):
    from cron import incidents

    executions = _point_ledger(monkeypatch, tmp_path)
    first = executions.create_execution("old", source="builtin")
    incidents.upsert_incident("old", "offline")
    path = executions._db_path()
    old_fingerprint = executions._initialized_files[path]
    moved = path.with_suffix(".preserved")
    path.rename(moved)
    replacement = executions.create_execution("new", source="builtin")
    incidents.upsert_incident("new", "offline")
    assert executions.get_execution(first["id"]) is None
    assert executions.get_execution(replacement["id"]) is not None
    assert executions._initialized_files[path] != old_fingerprint
    assert [row["job_id"] for row in incidents.list_incidents()] == ["new"]
    with sqlite3.connect(str(path)) as conn:
        conn.execute("DROP INDEX idx_executions_occurrence")
        conn.execute("DROP INDEX idx_cron_incidents_job")
    executions.get_execution(replacement["id"])
    incidents.list_incidents()
    with sqlite3.connect(str(path)) as conn:
        assert executions._schema_is_current(conn)
        assert incidents._schema_is_current(conn)


def test_execution_transitions_are_durable(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)

    claimed = executions.create_execution("job-1", source="builtin")
    assert claimed["status"] == "claimed"
    assert claimed["claimed_at"]
    assert claimed["started_at"] is None
    assert claimed["finished_at"] is None

    running = executions.mark_execution_running(claimed["id"])
    assert running["status"] == "running"
    assert running["started_at"]

    completed = executions.finish_execution(claimed["id"], success=True)
    assert completed["status"] == "completed"
    assert completed["finished_at"]
    assert completed["error"] is None

    persisted = executions.list_executions(job_id="job-1")
    assert persisted == [completed]


def test_execution_can_be_loaded_by_exact_attempt_id(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    first = executions.create_execution("same-job", source="builtin")
    second = executions.create_execution("same-job", source="builtin")

    assert executions.get_execution(first["id"]) == first
    assert executions.get_execution(second["id"]) == second
    assert executions.get_execution("missing") is None


def test_current_schema_skips_first_touch_ddl(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("existing", source="builtin")
    executions._initialized_files.clear()

    def unexpected_initialize(_conn):
        raise AssertionError("current schema must not run DDL on first process touch")

    monkeypatch.setattr(executions, "_initialize_schema", unexpected_initialize)
    assert executions.get_execution(record["id"]) == record


def test_execution_writes_retry_sqlite_contention_without_blocking_reads(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    baseline = executions.create_execution("baseline", source="builtin")
    db_path = str(executions.EXECUTIONS_FILE)
    ready = tmp_path / "holder-ready"
    release = tmp_path / "holder-release"
    blocked = threading.Event()
    done = threading.Event()
    errors = []

    holder_code = textwrap.dedent(
        f"""
        import pathlib, sqlite3, time
        conn = sqlite3.connect({db_path!r})
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('BEGIN IMMEDIATE')
        pathlib.Path({str(ready)!r}).write_text('1')
        for _ in range(500):
            if pathlib.Path({str(release)!r}).exists():
                break
            time.sleep(0.01)
        conn.rollback()
        conn.close()
        """
    )
    from cron import incidents

    real_connect = executions._connect

    def observed_connect():
        blocked.set()
        return real_connect()

    def write_execution():
        try:
            executions.create_execution("cross-process", source="builtin")
            incidents.upsert_incident("cross-process", "boom")
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    monkeypatch.setattr(executions, "_connect", observed_connect)
    holder = subprocess.Popen([sys.executable, "-c", holder_code])
    writer = threading.Thread(target=write_execution)
    try:
        for _ in range(1000):
            if ready.exists():
                break
            time.sleep(0.01)
        assert ready.exists(), "holder never acquired SQLite's write lock"

        writer.start()
        assert blocked.wait(10), "writer never entered its contended connection"
        assert not done.is_set(), "writer bypassed SQLite's write lock"

        before = time.monotonic()
        assert executions.get_execution(baseline["id"]) == baseline
        assert time.monotonic() - before < 2.0
    finally:
        release.write_text("1")
        holder.wait(timeout=15)
        writer.join(timeout=15)

    assert done.is_set(), "writer did not proceed after SQLite lock release"
    assert errors == []
    assert executions.latest_execution("cross-process") is not None
    assert any(row["job_id"] == "cross-process" for row in incidents.list_incidents())

    # Replacing the ledger under a live process invalidates the inode-keyed schema caches.
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(db_path + suffix).unlink(missing_ok=True)
    recreated = executions.create_execution("after-replace", source="builtin")
    incidents.upsert_incident("after-replace", "boom")
    assert executions.get_execution(recreated["id"]) is not None
    assert any(row["job_id"] == "after-replace" for row in incidents.list_incidents())


def test_delete_write_yields_after_closing_connection(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    events = []

    class DeleteConnection:
        def execute(self, sql, params=()):
            if sql == "PRAGMA journal_mode":
                return self
            assert sql in {"BEGIN IMMEDIATE", "COMMIT"}
            return None

        def fetchone(self):
            return ("delete",)

        def close(self):
            events.append("close")

    monkeypatch.setattr(executions.time, "sleep", lambda seconds: events.append(("sleep", seconds)))
    with executions._serialized_write(
        lambda: cast(sqlite3.Connection, DeleteConnection()),
        threading.RLock(),
        tmp_path / "executions.db",
    ):
        pass

    assert events == ["close", ("sleep", executions._DELETE_WRITE_YIELD_SECONDS)]


def test_write_retry_budget_is_shared_with_commit(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    clock = [0.0]

    class ContendedCommit:
        commit_calls = 0
        closed = False

        def execute(self, sql, params=()):
            if sql == "PRAGMA journal_mode":
                return self
            if sql == "BEGIN IMMEDIATE":
                clock[0] += 10.0
                return None
            if sql == "COMMIT":
                self.commit_calls += 1
                clock[0] += 6.0
                raise sqlite3.OperationalError("database is locked")
            if sql == "ROLLBACK":
                return None
            raise AssertionError(sql)

        def fetchone(self):
            return ("wal",)

        def close(self):
            self.closed = True

    conn = ContendedCommit()
    monkeypatch.setattr(executions.time, "monotonic", lambda: clock[0])
    with __import__("pytest").raises(sqlite3.OperationalError):
        with executions._serialized_write(
            lambda: cast(sqlite3.Connection, conn),
            threading.RLock(),
            tmp_path / "executions.db",
        ):
            pass

    assert conn.commit_calls == 1
    assert conn.closed is True


def test_fresh_external_handoff_is_not_recovered_before_worker_adopts(
    monkeypatch, tmp_path
):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("handoff-job", source="builtin")
    assert executions.mark_execution_handoff_pending(record["id"]) is not None

    monkeypatch.setattr(executions, "_PROCESS_ID", "replacement-gateway")
    monkeypatch.setattr(executions, "_owner_is_live", lambda _pid, _started: False)

    assert executions.recover_interrupted_executions() == 0
    assert executions.get_execution(record["id"])["status"] == "claimed"
    adopted = executions.adopt_claimed_execution(record["id"])
    assert adopted["status"] == "running"
    assert adopted["handoff_pending"] == 0


def test_stale_external_handoff_is_recovered_unknown(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("handoff-job", source="builtin")
    pending = executions.mark_execution_handoff_pending(record["id"])

    monkeypatch.setattr(executions, "_PROCESS_ID", "replacement-gateway")
    monkeypatch.setattr(executions, "_owner_is_live", lambda _pid, _started: False)
    monkeypatch.setattr(
        executions.time,
        "time",
        lambda: pending["handoff_started_at"]
        + executions.HANDOFF_ADOPTION_GRACE_SECONDS
        + 1,
    )

    assert executions.recover_interrupted_executions() == 1
    recovered = executions.get_execution(record["id"])
    assert recovered["status"] == "unknown"
    assert recovered["handoff_pending"] == 0


def test_recovery_does_not_overwrite_concurrent_worker_adoption(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("adoption-race", source="builtin")
    pending = executions.mark_execution_handoff_pending(record["id"])
    assert pending is not None
    monkeypatch.setattr(executions, "_PROCESS_ID", "replacement-scheduler")
    monkeypatch.setattr(
        executions.time,
        "time",
        lambda: pending["handoff_started_at"]
        + executions.HANDOFF_ADOPTION_GRACE_SECONDS
        + 1,
    )

    def adopt_while_liveness_is_checked(_pid, _started_at):
        monkeypatch.setattr(executions, "_PROCESS_ID", "external-worker")
        monkeypatch.setattr(executions.os, "getpid", lambda: 4242)
        monkeypatch.setattr(executions, "_process_start_time", lambda _pid: 9876)
        assert executions.adopt_claimed_execution(record["id"]) is not None
        return False

    monkeypatch.setattr(executions, "_owner_is_live", adopt_while_liveness_is_checked)

    assert executions.recover_interrupted_executions() == 0
    current = executions.get_execution(record["id"])
    assert current is not None
    assert current["status"] == "running"
    assert current["process_id"] == "external-worker"
    assert current["pid"] == 4242


def test_foreign_process_cannot_start_or_finish_execution(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("owner-fence", source="builtin")
    original_process_id = executions._PROCESS_ID
    original_pid = record["pid"]

    monkeypatch.setattr(executions, "_PROCESS_ID", "foreign-process")
    monkeypatch.setattr(executions.os, "getpid", lambda: original_pid + 1)
    assert executions.mark_execution_running(record["id"]) is None
    assert executions.finish_execution(record["id"], success=True) is None

    monkeypatch.setattr(executions, "_PROCESS_ID", original_process_id)
    monkeypatch.setattr(executions.os, "getpid", lambda: original_pid)
    assert executions.mark_execution_running(record["id"]) is not None
    assert executions.finish_execution(record["id"], success=True) is not None


def test_execution_ledger_follows_the_current_profile_home(monkeypatch, tmp_path):
    from cron import executions

    current_home = {"path": tmp_path / "default"}
    current_home["path"].mkdir()
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", None)
    monkeypatch.setattr(executions, "get_hermes_home", lambda: current_home["path"])

    default_row = executions.create_execution("default-job", source="builtin")
    current_home["path"] = tmp_path / "worker"
    current_home["path"].mkdir()
    worker_row = executions.create_execution("worker-job", source="builtin")

    assert executions.list_executions() == [worker_row]
    current_home["path"] = tmp_path / "default"
    assert executions.list_executions() == [default_row]
    assert (tmp_path / "default" / "cron" / "executions.db").is_file()
    assert (tmp_path / "worker" / "cron" / "executions.db").is_file()


def test_terminal_execution_cannot_be_rewritten(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("immutable", source="builtin")
    executions.mark_execution_running(record["id"])
    executions.finish_execution(record["id"], success=True)

    assert executions.finish_execution(
        record["id"], success=False, error="late writer"
    ) is None
    assert executions.latest_execution("immutable")["status"] == "completed"


def test_retention_bounds_terminal_history_but_preserves_inflight(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 3)
    inflight = executions.create_execution("live", source="builtin")
    executions.mark_execution_running(inflight["id"])
    for index in range(8):
        row = executions.create_execution(f"done-{index}", source="builtin")
        executions.finish_execution(row["id"], success=True)

    records = executions.list_executions(limit=100)
    assert len([row for row in records if row["status"] == "completed"]) == 3
    assert executions.latest_execution("live")["status"] == "running"


def test_recently_finished_long_running_execution_survives_retention(
    monkeypatch, tmp_path
):
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 1)
    long_running = executions.create_execution("long-running", source="builtin")
    assert executions.mark_execution_running(long_running["id"]) is not None
    newer = executions.create_execution("newer", source="builtin")
    assert executions.finish_execution(newer["id"], success=True) is not None

    finished = executions.finish_execution(long_running["id"], success=True)

    assert finished is not None
    assert finished["status"] == "completed"
    assert executions.get_execution(long_running["id"])["status"] == "completed"
    assert executions.get_execution(newer["id"]) is None


def test_corrupt_store_fails_closed_without_overwrite(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions.EXECUTIONS_FILE.parent.mkdir(parents=True)
    executions.EXECUTIONS_FILE.write_bytes(b"not a sqlite database")

    with __import__("pytest").raises(sqlite3.DatabaseError):
        executions.create_execution("new", source="builtin")
    assert executions.EXECUTIONS_FILE.read_bytes() == b"not a sqlite database"


def test_cron_runs_cli_prints_execution_history(monkeypatch, tmp_path, capsys):
    executions = _point_ledger(monkeypatch, tmp_path)
    row = executions.create_execution("cli-job", source="builtin")
    executions.finish_execution(row["id"], success=False, error="boom")
    from hermes_cli.cron import cron_runs

    cron_runs("cli-job", limit=10)

    output = capsys.readouterr().out
    assert row["id"] in output
    assert "failed" in output
    assert "boom" in output


def test_quick_backup_includes_execution_ledger():
    from hermes_cli.backup import _QUICK_STATE_FILES

    assert "cron/executions.db" in _QUICK_STATE_FILES


def test_failed_execution_keeps_error(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)

    record = executions.create_execution("job-2", source="external")
    failed = executions.finish_execution(record["id"], success=False, error="provider exploded")

    assert failed["status"] == "failed"
    assert failed["error"] == "provider exploded"


def test_recovery_does_not_mark_live_process_execution_unknown(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("still-live", source="builtin")
    executions.mark_execution_running(record["id"])

    assert executions.recover_interrupted_executions() == 0
    assert executions.latest_execution("still-live")["status"] == "running"


def test_restart_marks_interrupted_execution_unknown_without_requeue(tmp_path):
    """Real temp-HERMES_HOME subprocess restart: in-flight is audit-only unknown."""
    home = tmp_path / "home"
    home.mkdir()
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(repo)

    create = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cron.executions import create_execution, mark_execution_running; "
            "r=create_execution('restart-job', source='builtin'); "
            "mark_execution_running(r['id']); print(r['id'])",
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    execution_id = create.stdout.strip()

    recover = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from cron.executions import recover_interrupted_executions, list_executions; "
            "print(recover_interrupted_executions()); "
            "print(json.dumps(list_executions(job_id='restart-job'))) ",
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    lines = recover.stdout.strip().splitlines()
    assert lines[0] == "1"
    records = json.loads(lines[1])
    assert len(records) == 1
    assert records[0]["id"] == execution_id
    assert records[0]["status"] == "unknown"
    assert records[0]["finished_at"]
    assert "restart" in records[0]["error"].lower()
    # Recovery only classifies the old attempt. It must not manufacture a new
    # claimed record (which would imply an automatic retry).
    assert [r["status"] for r in records] == ["unknown"]


def test_generic_submit_failure_finishes_attempt_and_releases_guard(monkeypatch):
    from cron import scheduler

    class BrokenPool:
        def submit(self, _callable):
            raise ValueError("executor rejected")

    finished = []
    monkeypatch.setattr(
        scheduler, "create_execution",
        lambda *_args, **_kwargs: {"id": "exec-submit-fail"},
    )
    monkeypatch.setattr(
        scheduler, "finish_execution",
        lambda execution_id, **kwargs: finished.append((execution_id, kwargs)),
    )
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda: [{"id": "submit-fail"}])
    monkeypatch.setattr(scheduler, "claim_job_for_fire", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "_get_parallel_pool", lambda _workers: BrokenPool())

    assert scheduler.tick(verbose=False, sync=False) == 0
    assert finished == [
        ("exec-submit-fail", {
            "success": False,
            "error": "Executor dispatch failed: executor rejected",
        })
    ]
    assert "submit-fail" not in scheduler.get_running_job_ids()


def test_run_one_job_records_running_then_terminal(monkeypatch):
    from cron import scheduler

    events = []
    run_execution_ids = []
    monkeypatch.setattr(
        scheduler,
        "mark_execution_running",
        lambda execution_id: events.append(("running", execution_id)) or {},
        raising=False,
    )
    monkeypatch.setattr(
        scheduler,
        "finish_execution",
        lambda execution_id, **kwargs: events.append(("finish", execution_id, kwargs)),
        raising=False,
    )
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)

    def fake_run_job(job, *, defer_agent_teardown=None, execution_id=None, **_kw):
        run_execution_ids.append(execution_id)
        return True, "output", "response", None

    monkeypatch.setattr(scheduler, "run_job", fake_run_job)
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: None)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_args, **_kwargs: None)

    assert scheduler.run_one_job({"id": "job-3", "execution_id": "exec-3"}) is True
    assert run_execution_ids == ["exec-3"]
    assert events[0] == ("running", "exec-3")
    assert events[-1][0:2] == ("finish", "exec-3")
    assert events[-1][2]["success"] is True


def test_provider_start_recovers_interrupted_records_before_tick(monkeypatch):
    import cron.scheduler_provider as provider

    events = []
    stop = __import__("threading").Event()
    stop.set()
    monkeypatch.setattr(
        "cron.executions.recover_interrupted_executions",
        lambda: events.append("recover") or 0,
        raising=False,
    )
    monkeypatch.setattr("cron.jobs.record_ticker_heartbeat", lambda **_kwargs: events.append("heartbeat"))

    provider.InProcessCronScheduler().start(stop, interval=1)

    assert events[:2] == ["recover", "heartbeat"]


def test_external_provider_start_recovers_interrupted_records(monkeypatch):
    from plugins.cron_providers.chronos import ChronosCronScheduler

    provider = ChronosCronScheduler()
    provider._client = type("Client", (), {"arm": lambda self, **kwargs: None})()
    events = []
    monkeypatch.setattr(
        "cron.executions.recover_interrupted_executions",
        lambda: events.append("recover") or 0,
    )
    monkeypatch.setattr(provider, "reconcile", lambda: events.append("reconcile"))

    provider.start(__import__("threading").Event())

    assert events == ["recover", "reconcile"]


class _TrackingConnection:
    """Delegates to a real sqlite3.Connection while recording close() calls.

    sqlite3.Connection is a static C type: it has no per-instance __dict__
    and its class methods can't be monkeypatched, so open/close tracking is
    done via a delegating wrapper returned in place of the real connection.
    """

    def __init__(self, real, closed_ids):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_closed_ids", closed_ids)

    def close(self):
        self._closed_ids.append(id(self._real))
        self._real.close()

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        setattr(self._real, name, value)


def _count_open_connections(executions, monkeypatch):
    """Wrap sqlite3.connect to track open/close balance for the ledger module."""
    opened_ids = []
    closed_ids = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened_ids.append(id(conn))
        return _TrackingConnection(conn, closed_ids)

    monkeypatch.setattr(executions.sqlite3, "connect", tracking_connect)
    return opened_ids, closed_ids


def test_ledger_operations_close_every_connection(monkeypatch, tmp_path):
    """Regression for #69567: every ledger call must close its connection
    deterministically instead of relying on garbage collection."""
    executions = _point_ledger(monkeypatch, tmp_path)
    opened, closed = _count_open_connections(executions, monkeypatch)

    record = executions.create_execution("leak-check", source="builtin")
    executions.mark_execution_running(record["id"])
    executions.finish_execution(record["id"], success=True)
    executions.list_executions(job_id="leak-check")
    executions.latest_executions(["leak-check"])
    executions.recover_interrupted_executions()

    assert opened
    assert sorted(opened) == sorted(closed)


def test_early_return_still_closes_connection(monkeypatch, tmp_path):
    """mark_execution_running returns None mid-block on a bad transition;
    the connection must still be closed rather than leaked."""
    executions = _point_ledger(monkeypatch, tmp_path)
    opened, closed = _count_open_connections(executions, monkeypatch)

    assert executions.mark_execution_running("does-not-exist") is None

    assert len(opened) == 1
    assert len(closed) == 1






def test_job_listing_exposes_latest_execution(monkeypatch, tmp_path):
    from cron import jobs

    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    executions = _point_ledger(monkeypatch, tmp_path)

    job = jobs.create_job(prompt="audit me", schedule="every 1h", name="audit")
    record = executions.create_execution(job["id"], source="builtin")
    executions.mark_execution_running(record["id"])

    listed = jobs.list_jobs(include_disabled=True)
    assert listed[0]["latest_execution"]["id"] == record["id"]
    assert listed[0]["latest_execution"]["status"] == "running"


def test_history_orders_by_instant_across_dst_fall_back(monkeypatch, tmp_path):
    """01:10-05:00 is 20 minutes after 01:50-04:00 but sorts first as text."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    executions = _point_ledger(monkeypatch, tmp_path)
    new_york = ZoneInfo("America/New_York")
    first_pass = datetime(2026, 11, 1, 1, 50, tzinfo=new_york, fold=0)
    second_pass = datetime(2026, 11, 1, 1, 10, tzinfo=new_york, fold=1)
    monkeypatch.setattr(executions, "_hermes_now", lambda: first_pass)
    earlier = executions.create_execution("dst-job", source="builtin")
    monkeypatch.setattr(executions, "_hermes_now", lambda: second_pass)
    later = executions.create_execution("dst-job", source="builtin")
    assert later["claimed_at"] < earlier["claimed_at"]

    assert executions.latest_execution("dst-job")["id"] == later["id"]
    assert executions.latest_executions(["dst-job"])["dst-job"]["id"] == later["id"]
    assert [r["id"] for r in executions.list_executions(job_id="dst-job")] == [
        later["id"], earlier["id"],
    ]
    page = executions.list_executions(job_id="dst-job", before_claimed_at=later["claimed_at"])
    assert [r["id"] for r in page] == [earlier["id"]]

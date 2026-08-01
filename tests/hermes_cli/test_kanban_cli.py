"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Workspace flag parsing
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# run_slash smoke tests (end-to-end via the same entry both CLI and gateway use)
# ---------------------------------------------------------------------------



def test_kanban_list_json_includes_session_id(kanban_home):
    """JSON output exposes `session_id` so external clients (Scarf, web
    dashboards) don't need a side query to filter by chat session."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        kb.create_task(
            conn, title="acp task", assignee="alice", session_id="acp-x"
        )
    raw = kc.run_slash("list --json")
    payload = json.loads(raw)
    assert any(
        row.get("title") == "acp task"
        and row.get("session_id") == "acp-x"
        for row in payload
    )


def test_kanban_show_text_renders_graph_with_open_connection(kanban_home):
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parent task")
        child_id = kb.create_task(conn, title="child task")
        kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)

    output = kc.run_slash(f"show {child_id}")

    assert f"Task {child_id}: child task" in output
    assert f"parents:   {parent_id}" in output
    assert "Cannot operate on a closed database" not in output


def test_board_override_is_isolated_per_concurrent_call(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    barrier = threading.Barrier(2)
    original_init_db = kb.init_db

    def slow_init_db(*args, **kwargs):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return original_init_db(*args, **kwargs)

    monkeypatch.setattr(kb, "init_db", slow_init_db)

    failures: list[str] = []

    def worker(board: str, title: str) -> None:
        args = parser.parse_args(["kanban", "--board", board, "create", title])
        rc = kc.kanban_command(args)
        if rc != 0:
            failures.append(f"{board}:{rc}")

    t1 = threading.Thread(target=worker, args=("alpha", "alpha-task"))
    t2 = threading.Thread(target=worker, args=("beta", "beta-task"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert failures == []

    with kb.connect_closing(board="alpha") as conn:
        alpha_titles = [row.title for row in kb.list_tasks(conn, limit=100)]
    with kb.connect_closing(board="beta") as conn:
        beta_titles = [row.title for row in kb.list_tasks(conn, limit=100)]

    assert alpha_titles == ["alpha-task"]
    assert beta_titles == ["beta-task"]


def _notify_subs_for(task_id: str, *, board: str | None = None) -> list[dict]:
    with kb.connect_closing(board=board) as conn:
        subs = list(kb.list_notify_subs(conn, task_id))
    out = []
    for sub in subs:
        out.append(dict(sub) if isinstance(sub, dict) else sub.__dict__)
    return out


def _bind_gateway_session(**overrides):
    from gateway.session_context import reset_session_vars, set_session_vars

    reset_session_vars()
    params = {
        "platform": "telegram",
        "chat_id": "chat-42",
        "chat_type": "dm",
        "thread_id": "topic-7",
        "user_id": "user-9",
        "message_id": "msg-3",
        "profile": "gateway-profile",
    }
    params.update(overrides)
    return set_session_vars(**params)  # type: ignore[arg-type]


def _clear_gateway_session(tokens) -> None:
    from gateway.session_context import clear_session_vars, reset_session_vars

    clear_session_vars(tokens)
    reset_session_vars()


def test_create_json_auto_subscribes_messaging_origin_and_keeps_stdout_json(kanban_home):
    tokens = _bind_gateway_session()
    try:
        raw = kc.run_slash("create 'json origin task' --assignee worker --json")
    finally:
        _clear_gateway_session(tokens)

    payload = json.loads(raw)
    task_id = payload["id"]
    assert payload["title"] == "json origin task"

    subs = _notify_subs_for(task_id)
    assert len(subs) == 1
    sub = subs[0]
    assert sub["platform"] == "telegram"
    assert sub["chat_id"] == "chat-42"
    assert sub["chat_type"] == "dm"
    assert sub["thread_id"] == "topic-7"
    assert sub["user_id"] == "user-9"
    assert sub["notifier_profile"] == "gateway-profile"
    assert sub["delivery_metadata"]["chat_type"] == "dm"
    assert sub["delivery_metadata"]["telegram_reply_to_message_id"] == "msg-3"


def test_create_text_auto_subscribes_messaging_origin(kanban_home):
    tokens = _bind_gateway_session(platform="discord", chat_id="channel-7", thread_id="thread-2")
    try:
        raw = kc.run_slash("create 'text origin task' --assignee worker")
    finally:
        _clear_gateway_session(tokens)

    assert raw.startswith("Created t_"), raw
    task_id = raw.split()[1]
    subs = _notify_subs_for(task_id)
    assert [(s["platform"], s["chat_id"], s["thread_id"]) for s in subs] == [
        ("discord", "channel-7", "thread-2")
    ]


def test_create_does_not_subscribe_when_config_disabled(kanban_home):
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  auto_subscribe_on_create: false\n"
    )
    tokens = _bind_gateway_session()
    try:
        payload = json.loads(kc.run_slash("create 'disabled sub task' --json"))
    finally:
        _clear_gateway_session(tokens)

    assert _notify_subs_for(payload["id"]) == []


def test_create_does_not_subscribe_without_messaging_origin(kanban_home):
    from gateway.session_context import reset_session_vars

    reset_session_vars()
    payload = json.loads(kc.run_slash("create 'plain cli task' --json"))
    assert _notify_subs_for(payload["id"]) == []


def test_create_does_not_subscribe_non_messaging_origin(kanban_home):
    tokens = _bind_gateway_session(platform="api_server", chat_id="request-1")
    try:
        payload = json.loads(kc.run_slash("create 'api server task' --json"))
    finally:
        _clear_gateway_session(tokens)

    assert _notify_subs_for(payload["id"]) == []


def test_create_auto_subscribe_respects_explicit_board(kanban_home):
    kb.create_board("origin-board")
    tokens = _bind_gateway_session(platform="slack", chat_id="C123", thread_id="1729.1")
    try:
        payload = json.loads(
            kc.run_slash("--board origin-board create 'board origin task' --json")
        )
    finally:
        _clear_gateway_session(tokens)

    assert _notify_subs_for(payload["id"]) == []
    subs = _notify_subs_for(payload["id"], board="origin-board")
    assert [(s["platform"], s["chat_id"], s["thread_id"]) for s in subs] == [
        ("slack", "C123", "1729.1")
    ]


# ---------------------------------------------------------------------------
# Integration with the COMMAND_REGISTRY
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# reclaim + reassign CLI smoke tests
# ---------------------------------------------------------------------------

def test_run_slash_reclaim_running_task(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db as kb

    out1 = kc.run_slash("create 'stuck worker task' --assignee broken-model")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    tid = m.group(1)

    # Simulate a running claim outside TTL.
    conn = kb.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reclaim {tid} --reason 'test'")
    assert "Reclaimed" in out, out
    # Status back to ready.
    out2 = kc.run_slash(f"show {tid}")
    assert "ready" in out2.lower()




# ---------------------------------------------------------------------------
# /kanban specify — slash surface (same entry point CLI + gateway use)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /kanban help / no-args / unknown-action UX (issue #21794)
# ---------------------------------------------------------------------------



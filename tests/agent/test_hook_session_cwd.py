"""Hooks receive the session's workspace root, not the gateway process directory."""

from __future__ import annotations

from types import SimpleNamespace

from tools.terminal_tool import record_session_cwd


def test_pre_tool_call_receives_session_workspace(tmp_path, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda hook, **kw: seen.update(kw) or [])
    record_session_cwd("task-cwd-tool", str(tmp_path))
    from hermes_cli.plugins import _dispatch_pre_tool_call_hooks

    _dispatch_pre_tool_call_hooks("write_file", {"path": "a.txt"}, task_id="task-cwd-tool")
    assert seen["cwd"] == str(tmp_path)


def test_pre_llm_call_receives_session_workspace(tmp_path, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda hook, **kw: (seen.update(kw) if hook == "pre_llm_call" else None) or [],
    )
    record_session_cwd("task-cwd-llm", str(tmp_path))
    from agent.turn_context import _collect_pre_llm_call_context

    _collect_pre_llm_call_context(
        SimpleNamespace(session_id="s", model="m"), effective_task_id="task-cwd-llm",
        turn_id="t", original_user_message="hi", messages=[], conversation_history=[],
    )
    assert seen["cwd"] == str(tmp_path)

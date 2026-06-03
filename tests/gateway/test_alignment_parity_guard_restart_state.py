import importlib.util
from pathlib import Path


PARITY = Path("/home/krishna/.hermes/scripts/hermes_upstream_parity_guard.py")


def load_parity():
    spec = importlib.util.spec_from_file_location("upstream_parity_guard", PARITY)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gateway_restart_issue_marks_active_sessions_as_not_safe():
    mod = load_parity()

    item = mod.gateway_restart_issue(
        {
            "active_agents": 1,
            "active_agent_sessions": ["agent:main:discord:thread:1"],
            "activity_status_version": 1,
        },
        ref_mtime_epoch=2000,
        pid_start_epoch=1000,
        pid=123,
    )

    assert item["code"] == "gateway_may_need_restart"
    assert item["gateway_restart_safe"] is False
    assert item["gateway_restart_blocked_reason"] == "active_sessions"
    assert item["active_agents"] == 1
    assert item["active_agent_sessions"] == ["agent:main:discord:thread:1"]


def test_gateway_restart_issue_marks_missing_telemetry_as_not_safe():
    mod = load_parity()

    item = mod.gateway_restart_issue(
        {"active_agents": 0, "active_agent_sessions": []},
        ref_mtime_epoch=2000,
        pid_start_epoch=1000,
        pid=123,
    )

    assert item["gateway_restart_safe"] is False
    assert item["gateway_restart_blocked_reason"] == "telemetry_unavailable"
    assert item["activity_status_version"] == 0


def test_gateway_restart_issue_marks_idle_telemetry_as_safe():
    mod = load_parity()

    item = mod.gateway_restart_issue(
        {"active_agents": 0, "active_agent_sessions": [], "activity_status_version": 1},
        ref_mtime_epoch=2000,
        pid_start_epoch=1000,
        pid=123,
    )

    assert item["gateway_restart_safe"] is True
    assert item["gateway_restart_blocked_reason"] is None
    assert item["active_agents"] == 0
    assert item["active_agent_sessions"] == []

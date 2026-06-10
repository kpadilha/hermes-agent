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


# A path_classification fixture that simulates a runtime code change.
HOT_RUNTIME_COMMIT = {
    "hot": ["agent/run.py"],
    "cold": [],
    "unknown": [],
}

# A path_classification fixture that simulates a tooling-only commit.
COLD_TOOLING_COMMIT = {
    "hot": [],
    "cold": ["tools/tirith_security.py", "tests/tools/test_tirith_security.py"],
    "unknown": [],
}

# A path_classification fixture that simulates a change in unknown paths.
UNKNOWN_PATHS_COMMIT = {
    "hot": [],
    "cold": [],
    "unknown": ["some/external/thing.py"],
}


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
        path_classification=HOT_RUNTIME_COMMIT,
    )

    assert item is not None
    assert item["code"] == "gateway_may_need_restart"
    assert item["gateway_restart_safe"] is False
    assert item["gateway_restart_blocked_reason"] == "active_sessions"
    assert item["active_agents"] == 1
    assert item["active_agent_sessions"] == ["agent:main:discord:thread:1"]
    assert item["severity"] == "fail"
    assert item["hot_paths"] == ["agent/run.py"]


def test_gateway_restart_issue_marks_missing_telemetry_as_not_safe():
    mod = load_parity()

    item = mod.gateway_restart_issue(
        {"active_agents": 0, "active_agent_sessions": []},
        ref_mtime_epoch=2000,
        pid_start_epoch=1000,
        pid=123,
        path_classification=HOT_RUNTIME_COMMIT,
    )

    assert item is not None
    assert item["gateway_restart_safe"] is False
    assert item["gateway_restart_blocked_reason"] == "telemetry_unavailable"
    assert item["activity_status_version"] == 0
    assert item["severity"] == "fail"


def test_gateway_restart_issue_marks_idle_telemetry_as_safe():
    mod = load_parity()

    item = mod.gateway_restart_issue(
        {"active_agents": 0, "active_agent_sessions": [], "activity_status_version": 1},
        ref_mtime_epoch=2000,
        pid_start_epoch=1000,
        pid=123,
        path_classification=HOT_RUNTIME_COMMIT,
    )

    assert item is not None
    assert item["gateway_restart_safe"] is True
    assert item["gateway_restart_blocked_reason"] is None
    assert item["active_agents"] == 0
    assert item["active_agent_sessions"] == []
    assert item["severity"] == "fail"


def test_cold_only_paths_suppress_restart_issue():
    """A commit that touches only tooling/tests/docs must not trigger a
    gateway restart warning. This is the regression test for the false alarm
    caused by `d19ef33c7` (tools/tirith_security.py + test file only)."""
    mod = load_parity()

    item = mod.gateway_restart_issue(
        {"active_agents": 0, "active_agent_sessions": [], "activity_status_version": 1},
        ref_mtime_epoch=2000,
        pid_start_epoch=1000,
        pid=123,
        path_classification=COLD_TOOLING_COMMIT,
    )

    # Cold-only commits still emit at warn (so the operator sees the ref moved)
    # but the restart is marked safe and severity is warn, not fail.
    assert item is not None
    assert item["severity"] == "warn"
    assert item["gateway_restart_safe"] is True
    assert item["hot_paths"] == []
    assert item["cold_paths"] == sorted(COLD_TOOLING_COMMIT["cold"])


def test_empty_path_classification_returns_none():
    """No path classification means the ref moved but no file changed.
    This is the case for a fetch-only update of the upstream tracking ref.
    """
    mod = load_parity()

    item = mod.gateway_restart_issue(
        {"active_agents": 0, "active_agent_sessions": [], "activity_status_version": 1},
        ref_mtime_epoch=2000,
        pid_start_epoch=1000,
        pid=123,
        path_classification={"hot": [], "cold": [], "unknown": []},
    )

    assert item is None


def test_unknown_paths_emit_at_warn_by_default():
    """Paths not matched by hot or cold defaults go to `warn` so an operator
    can review and add them to the right classifier in the manifest."""
    mod = load_parity()

    item = mod.gateway_restart_issue(
        {"active_agents": 0, "active_agent_sessions": [], "activity_status_version": 1},
        ref_mtime_epoch=2000,
        pid_start_epoch=1000,
        pid=123,
        path_classification=UNKNOWN_PATHS_COMMIT,
    )

    assert item is not None
    assert item["severity"] == "warn"
    assert item["unknown_paths"] == UNKNOWN_PATHS_COMMIT["unknown"]


def test_unknown_paths_emit_at_fail_when_configured():
    mod = load_parity()

    item = mod.gateway_restart_issue(
        {"active_agents": 0, "active_agent_sessions": [], "activity_status_version": 1},
        ref_mtime_epoch=2000,
        pid_start_epoch=1000,
        pid=123,
        path_classification=UNKNOWN_PATHS_COMMIT,
        unknown_path_severity="fail",
    )

    assert item is not None
    assert item["severity"] == "fail"


def test_classify_paths_partitions_hot_cold_unknown():
    mod = load_parity()

    paths = [
        "agent/run.py",         # hot
        "tools/x.py",           # cold
        "tests/test_x.py",      # cold
        "external/thing.py",    # unknown
    ]
    result = mod.classify_paths(paths)

    assert result["hot"] == ["agent/run.py"]
    assert sorted(result["cold"]) == ["tests/test_x.py", "tools/x.py"]
    assert result["unknown"] == ["external/thing.py"]


def test_last_commit_paths_returns_head_minus_one_to_head():
    mod = load_parity()
    repo = Path("/home/krishna/.hermes/hermes-agent")

    # The most recent commit on production is d19ef33c7, which added
    # tools/tirith_security.py + tests/tools/test_tirith_security.py. Both
    # are cold paths; this verifies the helper targets the right range.
    paths = mod.last_commit_paths()
    assert isinstance(paths, list)
    assert "tools/tirith_security.py" in paths
    # We do not assert the exact set because HEAD might move, but the helper
    # must return a non-empty list whenever HEAD exists.
    assert paths

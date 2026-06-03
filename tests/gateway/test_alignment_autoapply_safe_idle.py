import importlib.util
from pathlib import Path
from types import SimpleNamespace


AUTOAPPLY = Path("/home/krishna/.hermes/scripts/hermes_alignment_candidate_autoapply.py")


def load_autoapply():
    spec = importlib.util.spec_from_file_location("alignment_autoapply", AUTOAPPLY)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_safe_idle_requires_new_activity_telemetry(monkeypatch):
    mod = load_autoapply()
    monkeypatch.setattr(mod, "gateway_health", lambda: {
        "pid": 123,
        "gateway_state": "running",
        "active_agents": 0,
        "updated_at": "2026-05-31T00:00:00Z",
    })

    ok, observations, reason = mod.wait_for_safe_gateway_idle(samples=1, interval=0)

    assert ok is False
    assert reason == "gateway_activity_telemetry_unavailable"
    assert observations[0]["activity_status_version"] is None


def test_safe_idle_blocks_active_agents(monkeypatch):
    mod = load_autoapply()
    monkeypatch.setattr(mod, "gateway_health", lambda: {
        "pid": 123,
        "gateway_state": "running",
        "active_agents": 1,
        "active_agent_sessions": ["agent:main:discord:thread:1"],
        "activity_status_version": 1,
    })

    ok, observations, reason = mod.wait_for_safe_gateway_idle(samples=1, interval=0)

    assert ok is False
    assert reason == "active_agents=1"
    assert observations[0]["active_agent_sessions"] == ["agent:main:discord:thread:1"]


def test_safe_idle_blocks_sessions_even_when_count_is_zero(monkeypatch):
    mod = load_autoapply()
    monkeypatch.setattr(mod, "gateway_health", lambda: {
        "pid": 123,
        "gateway_state": "running",
        "active_agents": 0,
        "active_agent_sessions": ["agent:main:telegram:dm:1"],
        "activity_status_version": 1,
    })

    ok, _observations, reason = mod.wait_for_safe_gateway_idle(samples=1, interval=0)

    assert ok is False
    assert reason == "active_agent_sessions_not_empty"


def test_safe_idle_blocks_non_running_gateway(monkeypatch):
    mod = load_autoapply()
    monkeypatch.setattr(mod, "gateway_health", lambda: {
        "pid": 123,
        "gateway_state": "starting",
        "active_agents": 0,
        "active_agent_sessions": [],
        "activity_status_version": 1,
    })

    ok, _observations, reason = mod.wait_for_safe_gateway_idle(samples=1, interval=0)

    assert ok is False
    assert reason == "gateway_not_running: starting"


def test_safe_idle_blocks_pid_change_between_samples(monkeypatch):
    mod = load_autoapply()
    responses = iter([
        {"pid": 123, "gateway_state": "running", "active_agents": 0, "active_agent_sessions": [], "activity_status_version": 1},
        {"pid": 456, "gateway_state": "running", "active_agents": 0, "active_agent_sessions": [], "activity_status_version": 1},
    ])
    monkeypatch.setattr(mod, "gateway_health", lambda: next(responses))

    ok, observations, reason = mod.wait_for_safe_gateway_idle(samples=2, interval=0)

    assert ok is False
    assert reason == "gateway_pid_changed_during_idle_probe"
    assert [item["pid"] for item in observations] == [123, 456]


def test_safe_idle_reports_health_probe_failure(monkeypatch):
    mod = load_autoapply()

    def fail():
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "gateway_health", fail)

    ok, observations, reason = mod.wait_for_safe_gateway_idle(samples=1, interval=0)

    assert ok is False
    assert observations == []
    assert reason.startswith("health_probe_failed: RuntimeError: boom")


def test_safe_idle_accepts_stable_zero_with_new_activity_telemetry(monkeypatch):
    mod = load_autoapply()
    monkeypatch.setattr(mod, "gateway_health", lambda: {
        "pid": 123,
        "gateway_state": "running",
        "active_agents": 0,
        "active_agent_sessions": [],
        "activity_status_version": 1,
        "updated_at": "2026-05-31T00:00:00Z",
    })

    ok, observations, reason = mod.wait_for_safe_gateway_idle(samples=2, interval=0)

    assert ok is True
    assert reason == "idle_stable"
    assert len(observations) == 2


def test_schedule_restart_defers_without_systemd_when_gate_blocks(monkeypatch):
    mod = load_autoapply()
    calls = []
    monkeypatch.setattr(mod, "wait_for_safe_gateway_idle", lambda: (False, [{"active_agents": 1}], "active_agents=1"))
    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    result = mod.schedule_gateway_restart()

    assert result["restart_deferred"] is True
    assert result["reason"] == "active_agents=1"
    assert result["rc"] == 0
    assert calls == []


def test_schedule_restart_script_revalidates_safe_idle_before_restart(monkeypatch):
    mod = load_autoapply()
    captured = {}

    monkeypatch.setattr(mod, "wait_for_safe_gateway_idle", lambda: (True, [{"pid": 123}], "idle_stable"))

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="queued")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    result = mod.schedule_gateway_restart(delay_seconds=5)

    assert result["rc"] == 0
    assert result["script_revalidates_safe_idle"] is True
    script = captured["cmd"][-1]
    assert "--safe-idle-check" in script
    assert "restart_deferred_by_safe_idle_revalidation" in script
    assert script.index("--safe-idle-check") < script.index("systemctl --user restart hermes-gateway")

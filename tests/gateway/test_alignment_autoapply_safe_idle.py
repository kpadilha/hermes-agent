import importlib.util
from pathlib import Path


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

import importlib.util
import json
import sys
from pathlib import Path


ORCH = Path("/home/krishna/.hermes/scripts/hermes_alignment_candidate_orchestrator.py")


def load_orchestrator():
    spec = importlib.util.spec_from_file_location("alignment_orchestrator", ORCH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_restart_only(monkeypatch, capsys, restart_result):
    mod = load_orchestrator()
    parity = {"status": "degraded", "issues": [{"code": "gateway_may_need_restart"}]}
    monkeypatch.setattr(sys, "argv", ["orchestrator", "--restart-only"])
    monkeypatch.setattr(mod, "read_parity", lambda: parity)
    monkeypatch.setattr(mod, "restart_gateway", lambda: restart_result)
    monkeypatch.setattr(mod, "git_facts", lambda: {"dirty": ""})
    monkeypatch.setattr(mod, "run", lambda *args, **kwargs: {"rc": 0, "output": "{}"})

    rc = mod.main()
    out = json.loads(capsys.readouterr().out)
    return rc, out


def test_restart_only_deferred_is_nonzero_corrective_action_not_done(monkeypatch, capsys):
    rc, out = run_restart_only(monkeypatch, capsys, {
        "restart_pending": True,
        "restart_deferred": True,
        "reason": "active_agents=1",
        "rc": 0,
        "parity_state": {"status": "degraded"},
    })

    assert rc == 3
    assert out["status"] == "restart_only_deferred"


def test_restart_only_scheduled_controlled_is_success(monkeypatch, capsys):
    rc, out = run_restart_only(monkeypatch, capsys, {
        "unit": "hermes-controlled-gateway-restart-test",
        "rc": 0,
        "parity_state": {"status": "degraded"},
    })

    assert rc == 0
    assert out["status"] == "restart_only_scheduled_controlled"


def test_restart_only_scheduling_failure_is_nonzero(monkeypatch, capsys):
    rc, out = run_restart_only(monkeypatch, capsys, {
        "rc": 1,
        "output": "systemd-run failed",
        "parity_state": {"status": "degraded"},
    })

    assert rc == 3
    assert out["status"] == "restart_only_scheduling_failed"

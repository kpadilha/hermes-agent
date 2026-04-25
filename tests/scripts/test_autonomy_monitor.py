import importlib.util
import json
from pathlib import Path


SCRIPT_PATH = Path.home() / ".hermes" / "scripts" / "autonomy_monitor.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("autonomy_monitor_script_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_build_status_extracts_memory_infra_and_kb_signals():
    module = _load_module()

    status = module.build_status(
        health_payload={"healthy": True, "summary": "21/21 OK", "failures": [], "warnings": []},
        reconcile_payload={
            "recommendations": [{"code": "honcho_card_missing_user_entries", "severity": "warn"}],
            "sources": {"graphiti": {"facts": 10}},
            "freshness": {"memvid_snapshot": {"status": "fresh"}},
            "runtime": {"ollama": {"nomic_loaded": False}},
        },
        kb_lint_payload={"returncode": 0, "stdout": "✓ KB is clean — zero issues found."},
        kb_index_payload={"returncode": 0, "stdout": "Done: 381 chunks indexed, 86 unchanged, 0 errors"},
    )

    assert status["overall"] == "ok"
    assert status["checks"]["memory"]["status"] == "ok"
    assert status["checks"]["infra"]["status"] == "ok"
    assert status["checks"]["kb"]["status"] == "ok"
    assert status["checks"]["memory"]["details"]["graph_facts"] == 10


def test_build_status_marks_fail_recommendation_as_degraded():
    module = _load_module()

    status = module.build_status(
        health_payload={"healthy": True, "summary": "21/21 OK", "failures": [], "warnings": []},
        reconcile_payload={"recommendations": [{"code": "ledger_active_conflict", "severity": "fail"}]},
        kb_lint_payload={"returncode": 0, "stdout": "clean"},
        kb_index_payload={"returncode": 0, "stdout": "0 errors"},
    )

    assert status["overall"] == "degraded"
    assert status["checks"]["memory"]["status"] == "degraded"
    assert "ledger_active_conflict" in status["checks"]["memory"]["details"]["fail_recommendations"]


def test_should_alert_only_on_first_run_or_degradation_change():
    module = _load_module()
    old = {
        "overall": "ok",
        "checks": {
            "memory": {"status": "ok"},
            "infra": {"status": "ok"},
            "kb": {"status": "ok"},
        },
    }
    same = json.loads(json.dumps(old))
    degraded = {
        "overall": "degraded",
        "checks": {
            "memory": {"status": "degraded"},
            "infra": {"status": "ok"},
            "kb": {"status": "ok"},
        },
    }
    recovered = {
        "overall": "ok",
        "checks": {
            "memory": {"status": "ok"},
            "infra": {"status": "ok"},
            "kb": {"status": "ok"},
        },
    }

    assert module.compute_alert_decision(None, same)["should_alert"] is True
    assert module.compute_alert_decision(old, same)["should_alert"] is False
    decision = module.compute_alert_decision(old, degraded)
    assert decision["should_alert"] is True
    assert decision["reason"] == "degradation"
    decision = module.compute_alert_decision(degraded, recovered)
    assert decision["should_alert"] is True
    assert decision["reason"] == "recovery"


def test_state_round_trip_and_cli_json(monkeypatch, tmp_path, capsys):
    module = _load_module()
    state_path = tmp_path / "state.json"

    def fake_collect():
        return module.build_status(
            health_payload={"healthy": True, "summary": "21/21 OK", "failures": [], "warnings": []},
            reconcile_payload={"recommendations": []},
            kb_lint_payload={"returncode": 0, "stdout": "clean"},
            kb_index_payload={"returncode": 0, "stdout": "Done: 1 chunks indexed, 0 errors"},
        )

    monkeypatch.setattr(module, "collect_status", fake_collect)
    rc = module.main(["--json", "--state", str(state_path), "--no-alert"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["alert"]["should_alert"] is True
    assert state_path.exists()
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["overall"] == "ok"



def test_send_telegram_alert_captures_healthcheck_output(monkeypatch):
    module = _load_module()
    calls = []

    class Result:
        returncode = 2
        stdout = "noisy health output"
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module.send_telegram_alert({"status": {"overall": "warn"}})

    assert calls
    args, kwargs = calls[0]
    assert "--alert" in args
    assert "--json" in args
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True



def test_run_json_parses_warning_exit_json(monkeypatch):
    module = _load_module()

    class Result:
        returncode = 2
        stdout = '{"healthy": false, "has_warnings": true}\n'
        stderr = ""

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Result())

    payload = module.run_json(["health_check.py", "--json"])

    assert payload["healthy"] is False
    assert payload["_nonzero_returncode"] == 2
    assert "_error" not in payload

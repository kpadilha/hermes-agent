import importlib.util
from pathlib import Path


SCRIPT_PATH = Path.home() / ".hermes" / "scripts" / "health_check.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("health_check_script_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_assess_architecture_dashboard_returns_ok_when_all_layers_green():
    module = _load_module()

    assessment = module.assess_architecture_dashboard({
        "hermes_acts": {"status": "ok"},
        "honcho_remembers": {"status": "ok"},
        "lcm_proves": {"status": "ok"},
        "overall": {"status": "ok"},
    })

    assert assessment["ok"] is True
    assert assessment["warn"] is False
    assert assessment["percents"] == {"act": 100, "memory": 100, "lcm": 100, "overall": 100}
    assert "overall=ok" in assessment["detail"]


def test_assess_architecture_dashboard_warns_when_lcm_is_unknown_but_memory_is_ok():
    module = _load_module()

    assessment = module.assess_architecture_dashboard({
        "hermes_acts": {"status": "ok"},
        "honcho_remembers": {"status": "ok"},
        "lcm_proves": {"status": "unknown"},
        "overall": {"status": "unknown"},
    })

    assert assessment["ok"] is False
    assert assessment["warn"] is True
    assert "lcm_proves=unknown" in assessment["detail"]


def test_check_gateway_health_uses_warn_path_for_unknown_proof(monkeypatch):
    module = _load_module()
    cr = module.CheckResult()

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: type("R", (), {
        "returncode": 0,
        "stdout": '{"architecture_dashboard": {"hermes_acts": {"status": "ok"}, "honcho_remembers": {"status": "ok"}, "lcm_proves": {"status": "unknown"}, "overall": {"status": "unknown"}}, "lcm_gateway": {"scorecard": {"runtime_health": "ok"}}}',
        "stderr": "",
    })())

    module.check_gateway_health_payload(cr)

    assert cr.warnings
    assert any(item["name"] == "architecture_dashboard" and item["status"] == "WARN" for item in cr.results)


def test_check_gateway_health_fails_when_gateway_proof_is_degraded(monkeypatch):
    module = _load_module()
    cr = module.CheckResult()

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: type("R", (), {
        "returncode": 0,
        "stdout": '{"architecture_dashboard": {"hermes_acts": {"status": "degraded"}, "honcho_remembers": {"status": "ok"}, "lcm_proves": {"status": "degraded"}, "overall": {"status": "degraded"}}, "lcm_gateway": {"scorecard": {"runtime_health": "degraded"}}}',
        "stderr": "",
    })())

    module.check_gateway_health_payload(cr)

    assert cr.failures
    assert any(item["name"] == "gateway_proof_health" and item["status"] == "FAIL" for item in cr.results)


def test_load_runtime_config_falls_back_when_yaml_module_missing(monkeypatch, tmp_path):
    module = _load_module()
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("model:\n  provider: openai-codex\n", encoding="utf-8")
    monkeypatch.setattr(module, "HERMES_HOME", tmp_path)
    monkeypatch.setattr(module, "yaml", None)

    class Result:
        returncode = 0
        stdout = '{"model": {"provider": "openai-codex"}}\n'
        stderr = ""

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Result())

    loaded = module.load_runtime_config()
    assert loaded["model"]["provider"] == "openai-codex"


def test_bootstrap_hermes_pythonpath_adds_repo_and_site_packages(monkeypatch, tmp_path):
    module = _load_module()
    repo = tmp_path / "hermes-agent"
    site = repo / "venv" / "lib" / "python3.11" / "site-packages"
    site.mkdir(parents=True)
    monkeypatch.setattr(module, "HERMES_AGENT_REPO", repo)
    monkeypatch.setattr(module.sys, "path", [])

    module.bootstrap_hermes_pythonpath()

    assert str(repo) in module.sys.path
    assert str(site) in module.sys.path


def test_run_python_in_venv_json_returns_parsed_payload(monkeypatch):
    module = _load_module()

    class Result:
        returncode = 0
        stdout = '{"ok": true, "value": 7}\n'
        stderr = ""

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Result())

    loaded = module.run_python_in_venv_json("print('ignored')")
    assert loaded == {"ok": True, "value": 7}
    assert module.LAST_VENV_PROBE_ERROR == ""


def test_run_python_in_venv_json_records_failure_detail(monkeypatch):
    module = _load_module()

    class Result:
        returncode = 1
        stdout = ""
        stderr = "Traceback: provider timeout"

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Result())

    loaded = module.run_python_in_venv_json("raise SystemExit(1)")

    assert loaded is None
    assert "exit 1" in module.LAST_VENV_PROBE_ERROR
    assert "provider timeout" in module.LAST_VENV_PROBE_ERROR


def test_chat_ping_surfaces_venv_probe_detail(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "run_python_in_venv_json", lambda code: None)
    monkeypatch.setattr(module, "LAST_VENV_PROBE_ERROR", "timeout after 30s")

    try:
        module._chat_ping("https://example.test/v1", "secret", "model", "provider")
    except RuntimeError as exc:
        assert "timeout after 30s" in str(exc)
    else:
        raise AssertionError("_chat_ping should raise on missing payload")


def test_add_vertex_threshold_checks_warns_for_unknown_scores():
    module = _load_module()
    cr = module.CheckResult()

    module.add_vertex_threshold_checks(cr, {"act": 100, "memory": 97, "lcm": 94, "overall": 95})

    statuses = {item["name"]: item["status"] for item in cr.results}
    assert statuses["vertex_act"] == "OK"
    assert statuses["vertex_memory"] == "OK"
    assert statuses["vertex_lcm"] == "WARN"
    assert statuses["vertex_overall"] == "OK"


def test_check_operational_silent_bug_audit_reports_ok(monkeypatch, tmp_path):
    module = _load_module()
    cr = module.CheckResult()
    repo = tmp_path / "repo"
    venv_python = repo / "venv" / "bin" / "python"
    audit_script = repo / "scripts" / "operational_silent_bug_audit.py"
    venv_python.parent.mkdir(parents=True)
    audit_script.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    audit_script.write_text("", encoding="utf-8")
    monkeypatch.setattr(module, "HERMES_AGENT_REPO", repo)

    class Result:
        returncode = 0
        stdout = '{"success": true, "finding_count": 0, "findings": []}\n'
        stderr = ""

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Result())

    module.check_operational_silent_bug_audit(cr)

    assert any(item["name"] == "operational_silent_bug_audit" and item["status"] == "OK" for item in cr.results)


def test_check_operational_silent_bug_audit_fails_on_findings(monkeypatch, tmp_path):
    module = _load_module()
    cr = module.CheckResult()
    repo = tmp_path / "repo"
    venv_python = repo / "venv" / "bin" / "python"
    audit_script = repo / "scripts" / "operational_silent_bug_audit.py"
    venv_python.parent.mkdir(parents=True)
    audit_script.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    audit_script.write_text("", encoding="utf-8")
    monkeypatch.setattr(module, "HERMES_AGENT_REPO", repo)

    class Result:
        returncode = 0
        stdout = '{"success": false, "finding_count": 1, "findings": [{"severity": "fail", "code": "ledger_all_records_fixed_limit"}]}\n'
        stderr = ""

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Result())

    module.check_operational_silent_bug_audit(cr)

    assert any(item["name"] == "operational_silent_bug_audit" and item["status"] == "FAIL" for item in cr.results)


def test_check_docker_fails_when_inspect_command_fails(monkeypatch):
    module = _load_module()
    cr = module.CheckResult()

    class Result:
        returncode = 1
        stdout = ""
        stderr = "Cannot connect to the Docker daemon"

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Result())

    module.check_docker(cr)

    assert cr.failures
    assert all(item["status"] == "FAIL" for item in cr.results if item["name"].startswith("docker_"))
    assert "Cannot connect to the Docker daemon" in cr.failures[0]


def test_check_docker_fails_when_health_inspect_fails(monkeypatch):
    module = _load_module()
    cr = module.CheckResult()
    calls = []

    class RunningResult:
        returncode = 0
        stdout = "true\n"
        stderr = ""

    class HealthFailure:
        returncode = 1
        stdout = ""
        stderr = "health template failed"

    def fake_run(args, **kwargs):
        calls.append(args)
        if len(calls) % 2 == 1:
            return RunningResult()
        return HealthFailure()

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module.check_docker(cr)

    assert cr.failures
    assert all(item["status"] == "FAIL" for item in cr.results if item["name"].startswith("docker_"))
    assert "health template failed" in cr.failures[0]

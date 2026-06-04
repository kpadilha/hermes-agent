import importlib.util
import json
import subprocess
import sys
from pathlib import Path
import pytest

ROOT = Path('/home/krishna')
HEALTH_PATH = ROOT / '.hermes/scripts/health_check.py'
RAW_SCRIPT_PATH = ROOT / '.hermes/scripts/kb_raw_intake_processor_cron.py'
SAFE_RESTART_PATH = ROOT / '.hermes/scripts/hermes_safe_gateway_restart.py'
ALIGNMENT_SCOUT_PATH = ROOT / '.hermes/scripts/hermes_upstream_alignment_scout.py'
ALIGNMENT_ORCH_PATH = ROOT / '.hermes/scripts/hermes_alignment_candidate_orchestrator.py'
AUTOAPPLY_CRON_PATH = ROOT / '.hermes/scripts/hermes_alignment_candidate_autoapply_cron.sh'
PARITY_GUARD_SH_PATH = ROOT / '.hermes/scripts/hermes_upstream_parity_guard.sh'
REPO_PYTHON = ROOT / '.hermes/hermes-agent/venv/bin/python'


def load_health_module():
    spec = importlib.util.spec_from_file_location('niko_health_check_phase1', HEALTH_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class DummyCheckResult:
    def __init__(self):
        self.added = []
        self.warned = []

    def add(self, name, ok, detail=''):
        self.added.append((name, ok, detail))
        return ok

    def warn(self, name, detail=''):
        self.warned.append((name, detail))
        return True


def test_analyze_cron_internal_warnings_detects_known_tool_failures(tmp_path):
    mod = load_health_module()
    log = tmp_path / 'agent.log'
    log.write_text(
        "2026-06-03 12:00:14 WARNING [cron_10e337] Tool terminal returned error: "
        "python3: can't open file '/bad/kb_maintenance.py'\n"
        "2026-06-03 12:29:07 WARNING [cron_d020] Tool session_search returned error: "
        "around_message_id 1 not in session_id abc\n",
        encoding='utf-8',
    )

    findings = mod.analyze_cron_internal_warnings([log], max_age_hours=48)

    assert findings['count'] == 2
    assert 'file_not_found' in findings['classes']
    assert 'session_search_mismatch' in findings['classes']
    assert findings['summary'].startswith('count=2')
    assert 'Action:' in findings['summary']


def test_analyze_cron_internal_warnings_classifies_runtime_path_failures(tmp_path):
    mod = load_health_module()
    log = tmp_path / 'agent.log'
    log.write_text(
        "2026-06-04 02:00:28 WARNING [cron_8fdc183388f1_20260604_020023] "
        "Tool read_file returned error: File not found: /home/krishna/gbrain/RESOLVER.md\n"
        "2026-06-04 02:15:04 WARNING [cron_8fdc183388f1_20260604_020023] "
        "Tool terminal returned error: env: ‘bun’: No such file or directory; exit_code\": 127\n",
        encoding='utf-8',
    )

    findings = mod.analyze_cron_internal_warnings([log], max_age_hours=48)

    assert findings['count'] == 2
    assert 'file_not_found' in findings['classes']
    assert 'command_not_found' in findings['classes']
    assert findings['jobs']['8fdc183388f1']['count'] == 2
    assert 'export the cron PATH explicitly' in findings['summary']


def test_check_gateway_drain_timeouts_warns_on_recent_journal_match(monkeypatch):
    mod = load_health_module()

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout='2026-06-03T10:40:59 WARNING gateway.run: Gateway drain timed out after 60.0s with 1 active agent(s)\n',
            stderr='',
        )

    monkeypatch.setattr(mod.subprocess, 'run', fake_run)
    cr = DummyCheckResult()

    mod.check_gateway_drain_timeouts(cr)

    assert cr.warned
    assert cr.warned[0][0] == 'gateway_drain_timeout_recent'
    assert 'active agent' in cr.warned[0][1]


def test_check_gateway_drain_timeouts_warns_when_journal_probe_fails(monkeypatch):
    mod = load_health_module()

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, stdout='', stderr='permission denied')

    monkeypatch.setattr(mod.subprocess, 'run', fake_run)
    cr = DummyCheckResult()

    mod.check_gateway_drain_timeouts(cr)

    assert cr.warned
    assert cr.warned[0][0] == 'gateway_drain_timeout_recent'
    assert 'journal probe failed' in cr.warned[0][1]


def test_analyze_cron_internal_warnings_ignores_non_cron_tool_errors(tmp_path):
    mod = load_health_module()
    log = tmp_path / 'agent.log'
    log.write_text(
        "2026-06-03 WARNING [session_user456] Tool session_search returned error: "
        "around_message_id 1 not in session_id abc; captured output mentioned [cron_abc] but was not the log context\n",
        encoding='utf-8',
    )

    findings = mod.analyze_cron_internal_warnings([log], max_age_hours=48)

    assert findings['count'] == 0
    assert findings['classes'] == []


def test_analyze_cron_internal_warnings_dedupes_same_event_across_files(tmp_path):
    mod = load_health_module()
    line = "2026-06-03 WARNING [cron_abc] Tool session_search returned error: around_message_id 1 not in session_id abc\n"
    log1 = tmp_path / 'gateway.log'
    log2 = tmp_path / 'agent.log'
    log1.write_text(line, encoding='utf-8')
    log2.write_text(line, encoding='utf-8')

    findings = mod.analyze_cron_internal_warnings([log1, log2], max_age_hours=48)

    assert findings['count'] == 1


def test_safe_gateway_restart_probe_blocks_active_agents(tmp_path):
    payload = {
        'status': 'ok',
        'gateway_state': 'running',
        'active_agents': 1,
        'active_agent_sessions': ['agent:main:discord:thread:x:y'],
        'activity_status_version': 1,
        'pid': 123,
    }
    p = tmp_path / 'health.json'
    p.write_text(json.dumps(payload), encoding='utf-8')

    proc = subprocess.run(
        [sys.executable, str(SAFE_RESTART_PATH), '--health-json-file', str(p), '--probe-only'],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    assert proc.returncode != 0
    assert 'active_agents=1' in proc.stdout


def test_kb_raw_intake_empty_inbox_runs_deterministic_maintenance(tmp_path):
    vault = tmp_path / 'Krishna'
    kb = vault / 'kb'
    raw = kb / 'raw'
    raw.mkdir(parents=True)
    (raw / '.processed').mkdir()
    for script_name in ('kb_maintenance.py', 'kb_lint.py', 'kb_search.py'):
        script = kb / script_name
        if script_name == 'kb_search.py':
            script.write_text("import sys; print('indexed'); sys.exit(0)\n", encoding='utf-8')
        else:
            script.write_text("print('ok')\n", encoding='utf-8')

    proc = subprocess.run(
        [
            sys.executable,
            str(RAW_SCRIPT_PATH),
            '--vault-root',
            str(vault),
            '--skip-sync',
            '--force-output',
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    assert proc.returncode == 0
    assert 'pending=0' in proc.stdout
    assert 'validation=ok' in proc.stdout


def test_kb_raw_intake_pending_files_fail_closed(tmp_path):
    vault = tmp_path / 'Krishna'
    raw = vault / 'kb/raw'
    raw.mkdir(parents=True)
    (raw / 'note.txt').write_text('needs semantic processing', encoding='utf-8')

    proc = subprocess.run(
        [sys.executable, str(RAW_SCRIPT_PATH), '--vault-root', str(vault), '--skip-sync'],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    assert proc.returncode != 0
    assert 'pending raw files require semantic processing' in proc.stdout


def test_alignment_scout_direct_invocation_reexecs_to_repo_venv_python():
    if not REPO_PYTHON.exists():
        pytest.skip('Hermes repo venv python unavailable')
    proc = subprocess.run(
        ['/usr/bin/python3', str(ALIGNMENT_SCOUT_PATH), '--preflight-only'],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stdout
    payload = json.loads(proc.stdout)
    assert payload['python'] == str(REPO_PYTHON.resolve())
    assert payload['pyyaml'] is True


def test_alignment_orchestrator_uses_repo_python_for_scout_and_parity():
    text = ALIGNMENT_ORCH_PATH.read_text(encoding='utf-8')

    assert 'PY_BIN = repo_python()' in text
    assert 'run([PY_BIN, str(SCOUT)]' in text
    assert 'run([PY_BIN, str(PARITY)]' in text


def test_alignment_shell_wrappers_prefer_repo_venv_before_python3():
    for path in (AUTOAPPLY_CRON_PATH, PARITY_GUARD_SH_PATH):
        text = path.read_text(encoding='utf-8')
        assert '"$REPO/venv/bin/python"' in text
        assert 'python3' in text
        assert text.index('"$REPO/venv/bin/python"') < text.index('python3')

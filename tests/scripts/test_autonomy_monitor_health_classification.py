import importlib.util
from pathlib import Path

ROOT = Path('/home/krishna')
AUTONOMY_PATH = ROOT / '.hermes/scripts/autonomy_monitor.py'


def load_autonomy_module():
    spec = importlib.util.spec_from_file_location('niko_autonomy_monitor_health_classification', AUTONOMY_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_parseable_health_warning_returncode_is_not_execution_error():
    mod = load_autonomy_module()
    health_payload = {
        '_nonzero_returncode': 2,
        'healthy': False,
        'has_warnings': True,
        'summary': '28/30 OK, 2 WARN',
        'failures': [],
        'warnings': [
            'gateway_drain_timeout_recent: count=1, last=2026-06-03T16:55:36+02:00 ...',
            'cron_internal_warnings: count=1, classes=[\'tool_error\']',
        ],
    }

    status = mod.build_status(
        health_payload=health_payload,
        reconcile_payload={'recommendations': [], 'sources': {'graphiti': {'facts': 1}}},
        kb_lint_payload={'returncode': 0, 'stdout': 'ok', 'stderr': ''},
        kb_index_payload={'returncode': 0, 'stdout': 'indexed', 'stderr': ''},
    )

    infra = status['checks']['infra']
    assert infra['status'] == 'warn'
    assert infra['details']['failure_codes'] == []
    assert 'health_check_execution_error' not in infra['details']['warning_codes']
    assert 'gateway_drain_timeout_recent' in infra['details']['warning_codes']
    assert 'cron_internal_warnings' in infra['details']['warning_codes']


def test_health_protocol_failure_remains_execution_error():
    mod = load_autonomy_module()
    health_payload = {
        '_error': True,
        'returncode': 2,
        'stdout_tail': 'not json',
        'stderr': 'JSON protocol failure',
    }

    status = mod.build_status(
        health_payload=health_payload,
        reconcile_payload={'recommendations': [], 'sources': {'graphiti': {'facts': 1}}},
        kb_lint_payload={'returncode': 0, 'stdout': 'ok', 'stderr': ''},
        kb_index_payload={'returncode': 0, 'stdout': 'indexed', 'stderr': ''},
    )

    infra = status['checks']['infra']
    assert infra['status'] == 'degraded'
    assert 'health_check_execution_error' in infra['details']['failure_codes']

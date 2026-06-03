"""Code identity follows the status owner, not an out-of-process writer."""
import json

import pytest
from gateway import status


@pytest.mark.parametrize('recovered_sha', [None, 'boot-recovered'])
def test_recovered_owner_does_not_inherit_stale_code(tmp_path, monkeypatch, recovered_sha):
    path = tmp_path / 'gateway_state.json'
    path.write_text(json.dumps({'pid': 10, 'start_time': 1, 'code_sha': 'stale', 'code_version': 'stale'}))
    monkeypatch.setattr(status, '_get_runtime_status_path', lambda: path)
    monkeypatch.setattr(status, '_runtime_status_state', None)
    monkeypatch.setattr(status, '_current_process_is_gateway_runtime', lambda: False)
    monkeypatch.setattr(status, '_runtime_status_identity_is_live_gateway', lambda _: False)
    monkeypatch.setattr(status, '_build_pid_record', lambda: {'pid': 30, 'argv': ['health'], 'start_time': 3})
    monkeypatch.setattr(status, '_get_live_gateway_identity_record', lambda: {
        'pid': 20, 'start_time': 2, 'argv': ['gateway', 'run'], 'code_sha': recovered_sha})
    monkeypatch.setattr(status, '_get_code_identity_fields', lambda: pytest.fail('CLI code is not boot identity'))
    _, payload, _ = status._prepare_runtime_status_update(lcm_gateway={'fixture': True})
    assert (payload['pid'], payload['start_time']) == (20, 2)
    assert payload['code_sha'] == recovered_sha
    assert payload['code_version'] is None


def test_cli_initial_record_does_not_claim_gateway_code(monkeypatch):
    monkeypatch.setattr(status, '_current_process_is_gateway_runtime', lambda: False)
    monkeypatch.setattr(status, '_build_pid_record', lambda: {'pid': 30, 'argv': ['health'], 'start_time': 3})
    monkeypatch.setattr(status, '_get_code_identity_fields', lambda: pytest.fail('CLI code is not boot identity'))
    assert 'code_sha' not in status._build_runtime_status_record()


def test_runtime_still_stamps_its_own_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(status, '_get_runtime_status_path', lambda: tmp_path / 'gateway_state.json')
    monkeypatch.setattr(status, '_runtime_status_state', None)
    monkeypatch.setattr(status, '_current_process_is_gateway_runtime', lambda: True)
    monkeypatch.setattr(status, '_build_pid_record', lambda: {'pid': 20, 'argv': ['gateway', 'run'], 'start_time': 2})
    monkeypatch.setattr(status, '_get_code_identity_fields', lambda: {'code_sha': 'runtime-boot', 'code_version': 'runtime-version'})
    _, payload, _ = status._prepare_runtime_status_update()
    assert payload['code_sha'] == 'runtime-boot'
    assert payload['pid'] == 20

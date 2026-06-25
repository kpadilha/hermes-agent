from pathlib import Path

from scripts.operational_silent_bug_audit import audit_repo


def _write_minimal_repo(root: Path, *, paginated: bool, gated: bool, finite_ledger: bool = False, rowcount_guarded: bool = True) -> None:
    (root / "hermes_cli").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "agent").mkdir(parents=True)
    if paginated:
        reconcile = 'endpoint = f"/conclusions/list?size={size}&page={page}"\nwhile True:\n    break\n'
    else:
        reconcile = 'endpoint = "/conclusions/list?size=100"\n'
    reconcile += 'def _safe_discover(label, discoverer):\n    return discoverer(), None\n'
    if finite_ledger:
        reconcile += 'ledger_records = ledger.search("", limit=10000)\n'
    (root / "hermes_cli" / "memory_reconcile_cmd.py").write_text(reconcile, encoding="utf-8")
    for name in ["memory_graph_cmd.py", "memory_snapshot_cmd.py", "memory_ledger_cmd.py"]:
        (root / "hermes_cli" / name).write_text("records = ledger.list_records()\n", encoding="utf-8")
    ledger_text = "def _require_row_updated(cursor):\n    return cursor.rowcount\n" if rowcount_guarded else "def update_record():\n    pass\n"
    (root / "agent" / "memory_ledger.py").write_text(ledger_text, encoding="utf-8")
    if gated:
        bridge = 'if result.get("success") is True:\n    self._memory_manager.on_memory_write("add", "user", "x")\n'
    else:
        bridge = 'self._memory_manager.on_memory_write("add", "user", "x")\n'
    (root / "run_agent.py").write_text(bridge, encoding="utf-8")


def test_operational_audit_accepts_guarded_patterns(tmp_path):
    _write_minimal_repo(tmp_path, paginated=True, gated=True)

    assert audit_repo(tmp_path) == []


def test_operational_audit_accepts_guarded_patterns_in_extracted_local_memory_ops(tmp_path):
    _write_minimal_repo(tmp_path, paginated=False, gated=True, rowcount_guarded=False)
    local_ops = tmp_path / "hermes_cli" / "local_memory_ops"
    local_ops.mkdir(parents=True)
    (local_ops / "reconcile_cmd.py").write_text(
        'endpoint = f"/conclusions/list?size={size}&page={page}"\nwhile True:\n    break\n',
        encoding="utf-8",
    )
    (local_ops / "ledger.py").write_text(
        "def _require_row_updated(cursor):\n    return cursor.rowcount\n",
        encoding="utf-8",
    )
    for name in ["graph_cmd.py", "snapshot_cmd.py", "ledger_cmd.py"]:
        (local_ops / name).write_text("records = ledger.list_records()\n", encoding="utf-8")

    assert audit_repo(tmp_path) == []


def test_operational_audit_flags_unpaginated_honcho_and_unguarded_bridge(tmp_path):
    _write_minimal_repo(tmp_path, paginated=False, gated=False)

    codes = {finding.code for finding in audit_repo(tmp_path)}

    assert "honcho_conclusions_not_paginated" in codes
    assert "memory_projection_not_success_gated" in codes


def test_operational_audit_flags_all_record_search_with_fixed_limit(tmp_path):
    _write_minimal_repo(tmp_path, paginated=True, gated=True, finite_ledger=True)

    findings = audit_repo(tmp_path)

    assert any(f.code == "ledger_all_records_fixed_limit" for f in findings)


def test_operational_audit_flags_ledger_mutations_without_rowcount_guard(tmp_path):
    _write_minimal_repo(tmp_path, paginated=True, gated=True, rowcount_guarded=False)

    findings = audit_repo(tmp_path)

    assert any(f.code == "ledger_mutations_not_rowcount_checked" for f in findings)


def test_operational_audit_flags_silent_memory_reconcile_discovery_failure(tmp_path):
    _write_minimal_repo(tmp_path, paginated=True, gated=True)
    reconcile = tmp_path / "hermes_cli" / "memory_reconcile_cmd.py"
    reconcile.write_text(reconcile.read_text(encoding="utf-8") + "\ntry:\n    pass\nexcept Exception:\n    return []\n", encoding="utf-8")

    findings = audit_repo(tmp_path)

    assert any(f.code == "memory_reconcile_silent_discovery_failure" for f in findings)

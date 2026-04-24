import json
from pathlib import Path
from types import SimpleNamespace

from agent.memory_ledger import BeliefLedger, MemoryWriteGate
from hermes_cli.memory_reconcile_cmd import (
    build_memory_reconcile_report,
    build_fix_plan,
    memory_reconcile_command,
    sync_honcho_peer_card_from_user_md,
    sync_honcho_conclusions_from_user_md,
)


def test_build_memory_reconcile_report_reports_sources_and_recommendations(tmp_path):
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "USER.md").write_text("Krishna prefers local-first memory.\n§\nKrishna uses Ollama.", encoding="utf-8")
    ledger = BeliefLedger(tmp_path / "ledger.db")
    MemoryWriteGate(ledger).evaluate_and_record(
        target="user",
        content="Krishna prefers local-first memory.",
        source="test",
        evidence_ref="test#1",
    )
    report = build_memory_reconcile_report(
        memory_dir=memories,
        ledger=ledger,
        graph_facts=[{"subject": "Krishna", "predicate": "prefers", "object": "local-first memory"}],
        honcho_card=["Krishna prefers local-first memory."],
        honcho_conclusions=[],
        ollama_models=[{"name": "phi4-mini:latest"}, {"name": "qwen3.5:latest"}],
        snapshot_wrappers=[tmp_path / "memory-ledger-mv2.md"],
        honcho_env={"HONCHO_UNLOAD_EMBEDDING_MODEL_AFTER_REQUEST": "true"},
    )

    assert report["success"] is True
    assert report["sources"]["user_md"]["count"] == 2
    assert report["sources"]["ledger"]["records"] >= 1
    assert report["sources"]["graphiti"]["facts"] == 1
    assert report["runtime"]["ollama"]["phi4_loaded"] is True
    assert report["runtime"]["ollama"]["qwen35_loaded"] is True
    assert report["runtime"]["honcho"]["unload_embedding_after_request"] is True
    assert any(r["code"] == "honcho_conclusions_missing_user_entries" for r in report["recommendations"])


def test_memory_reconcile_command_outputs_json(tmp_path, capsys):
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "USER.md").write_text("Krishna prefers local-first memory.", encoding="utf-8")
    ledger = BeliefLedger(tmp_path / "ledger.db")

    memory_reconcile_command(
        SimpleNamespace(json=True, fix=False, dry_run=False),
        memory_dir=memories,
        ledger=ledger,
        graph_facts=[],
        honcho_card=[],
        honcho_conclusions=[],
        ollama_models=[],
        snapshot_wrappers=[],
        honcho_env={},
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert "divergence" in payload


def test_build_fix_plan_is_dry_run_only_and_lists_safe_mutations(tmp_path):
    report = {
        "recommendations": [
            {"code": "honcho_card_missing_user_entries", "severity": "warn"},
            {"code": "honcho_conclusions_missing_user_entries", "severity": "info"},
            {"code": "graphiti_projection_stale", "severity": "warn"},
            {"code": "ollama_embedding_model_resident", "severity": "warn"},
            {"code": "ollama_phi4_cron_model_not_resident", "severity": "info"},
        ],
        "divergence": {
            "user_missing_in_honcho_card": ["Krishna prefers local-first memory."],
            "user_missing_in_honcho_conclusions": ["Krishna uses Ollama."],
            "ledger_missing_in_graphiti": ["local-first memory"],
        },
        "runtime": {"ollama": {"nomic_loaded": True}},
    }

    plan = build_fix_plan(report, dry_run=True)

    assert plan["dry_run"] is True
    assert plan["apply_supported"] is False
    action_ids = [action["id"] for action in plan["proposed_actions"]]
    assert "sync_honcho_peer_card_from_user_md" in action_ids
    assert "add_missing_honcho_conclusions" in action_ids
    assert "sync_memory_ledger_to_graphiti" in action_ids
    assert "unload_transient_ollama_embedding_model" in action_ids
    assert "load_phi4_cron_model" not in action_ids
    assert all(action["mutates"] is False for action in plan["proposed_actions"])


def test_memory_reconcile_fix_requires_dry_run(tmp_path):
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "USER.md").write_text("Krishna prefers local-first memory.", encoding="utf-8")
    ledger = BeliefLedger(tmp_path / "ledger.db")

    try:
        memory_reconcile_command(
            SimpleNamespace(json=True, fix=True, dry_run=False),
            memory_dir=memories,
            ledger=ledger,
            graph_facts=[],
            honcho_card=[],
            honcho_conclusions=[],
            ollama_models=[],
            snapshot_wrappers=[],
            honcho_env={},
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("--fix without --dry-run should exit")


def test_memory_reconcile_fix_dry_run_outputs_plan(tmp_path, capsys):
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "USER.md").write_text("Krishna prefers local-first memory.", encoding="utf-8")
    ledger = BeliefLedger(tmp_path / "ledger.db")

    memory_reconcile_command(
        SimpleNamespace(json=True, fix=True, dry_run=True),
        memory_dir=memories,
        ledger=ledger,
        graph_facts=[],
        honcho_card=[],
        honcho_conclusions=[],
        ollama_models=[{"name": "nomic-embed-text:latest"}],
        snapshot_wrappers=[tmp_path / "memory-ledger-mv2.md"],
        honcho_env={"HONCHO_UNLOAD_EMBEDDING_MODEL_AFTER_REQUEST": "true"},
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["fix_plan"]["dry_run"] is True
    assert payload["fix_plan"]["proposed_actions"]


def test_sync_honcho_peer_card_from_user_md_writes_and_readbacks(tmp_path):
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "USER.md").write_text("Name: Krishna\n§\nTimezone: Europe/Zurich", encoding="utf-8")
    calls = []

    def fake_http(method, url, payload=None, timeout=10):
        calls.append({"method": method, "url": url, "payload": payload})
        if method == "PUT":
            return {"peer_card": payload["peer_card"]}
        if method == "GET":
            return {"peer_card": ["Name: Krishna", "Timezone: Europe/Zurich"]}
        raise AssertionError(method)

    result = sync_honcho_peer_card_from_user_md(
        memory_dir=memories,
        peer_id="96809052",
        workspace_id="niko-main",
        base_url="http://honcho.test/v3",
        http_json=fake_http,
        dry_run=False,
    )

    assert result["success"] is True
    assert result["dry_run"] is False
    assert result["validation"]["card_matches_local"] is True
    assert result["written_count"] == 2
    assert calls[0]["method"] == "PUT"
    assert calls[1]["method"] == "GET"
    assert calls[0]["url"] == "http://honcho.test/v3/workspaces/niko-main/peers/96809052/card"


def test_sync_honcho_peer_card_dry_run_does_not_call_put(tmp_path):
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "USER.md").write_text("Name: Krishna", encoding="utf-8")
    calls = []

    def fake_http(method, url, payload=None, timeout=10):
        calls.append(method)
        return {"peer_card": []}

    result = sync_honcho_peer_card_from_user_md(
        memory_dir=memories,
        peer_id="96809052",
        workspace_id="niko-main",
        base_url="http://honcho.test/v3",
        http_json=fake_http,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["would_write_count"] == 1
    assert calls == []


def test_memory_reconcile_apply_peer_card_outputs_readback_validation(tmp_path, capsys):
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "USER.md").write_text("Name: Krishna", encoding="utf-8")
    ledger = BeliefLedger(tmp_path / "ledger.db")

    def fake_sync(**kwargs):
        return {"success": True, "validation": {"card_matches_local": True}, "written_count": 1}

    memory_reconcile_command(
        SimpleNamespace(json=True, fix=False, dry_run=False, apply_action="sync_honcho_peer_card_from_user_md", honcho_peer="96809052"),
        memory_dir=memories,
        ledger=ledger,
        graph_facts=[],
        honcho_card=[],
        honcho_conclusions=[],
        ollama_models=[],
        snapshot_wrappers=[],
        honcho_env={},
        peer_card_syncer=fake_sync,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["apply_result"]["success"] is True
    assert payload["apply_result"]["validation"]["card_matches_local"] is True


def test_sync_honcho_conclusions_from_user_md_dry_run_lists_missing(tmp_path):
    from hermes_cli.memory_reconcile_cmd import sync_honcho_conclusions_from_user_md
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "USER.md").write_text("Name: Krishna\n§\nTimezone: Europe/Zurich", encoding="utf-8")

    result = sync_honcho_conclusions_from_user_md(
        memory_dir=memories,
        existing_conclusions=["Name: Krishna"],
        observer_id="niko",
        observed_id="96809052",
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["would_create_count"] == 1
    assert result["conclusions"] == ["Timezone: Europe/Zurich"]


def test_sync_honcho_conclusions_from_user_md_applies_and_validates_visibility(tmp_path):
    from hermes_cli.memory_reconcile_cmd import sync_honcho_conclusions_from_user_md
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "USER.md").write_text("Name: Krishna\n§\nTimezone: Europe/Zurich", encoding="utf-8")
    created = []

    def fake_http(method, url, payload=None, timeout=10):
        if method == "POST" and url.endswith("/conclusions"):
            created.extend(item["content"] for item in payload["conclusions"])
            return [{"content": item["content"], "id": str(i)} for i, item in enumerate(payload["conclusions"])]
        if method == "POST" and "/conclusions/list" in url:
            return {"items": [{"content": item, "observer_id": "niko", "observed_id": "96809052"} for item in created]}
        raise AssertionError((method, url, payload))

    result = sync_honcho_conclusions_from_user_md(
        memory_dir=memories,
        existing_conclusions=[],
        observer_id="niko",
        observed_id="96809052",
        base_url="http://honcho.test/v3",
        workspace_id="niko-main",
        http_json=fake_http,
        dry_run=False,
    )

    assert result["success"] is True
    assert result["created_count"] == 2
    assert result["validation"]["all_visible"] is True
    assert result["validation"]["missing_after_write"] == []


def test_memory_reconcile_apply_conclusions_outputs_visibility_validation(tmp_path, capsys):
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "USER.md").write_text("Name: Krishna", encoding="utf-8")
    ledger = BeliefLedger(tmp_path / "ledger.db")

    def fake_sync(**kwargs):
        return {"success": True, "created_count": 1, "validation": {"all_visible": True}}

    memory_reconcile_command(
        SimpleNamespace(json=True, fix=False, dry_run=False, apply_action="add_missing_honcho_conclusions", honcho_peer="96809052"),
        memory_dir=memories,
        ledger=ledger,
        graph_facts=[],
        honcho_card=["Name: Krishna"],
        honcho_conclusions=[],
        ollama_models=[],
        snapshot_wrappers=[],
        honcho_env={},
        conclusion_syncer=fake_sync,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["apply_result"]["success"] is True
    assert payload["apply_result"]["validation"]["all_visible"] is True


def test_memory_reconcile_refreshes_lcm_memory_runtime_status(tmp_path, capsys):
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "USER.md").write_text("Name: Krishna", encoding="utf-8")
    ledger = BeliefLedger(tmp_path / "ledger.db")
    writes = []

    memory_reconcile_command(
        SimpleNamespace(json=True, fix=False, dry_run=False, apply_action="", honcho_peer="96809052"),
        memory_dir=memories,
        ledger=ledger,
        graph_facts=[{"object": "Name: Krishna"}],
        honcho_card=["Name: Krishna"],
        honcho_conclusions=["Name: Krishna"],
        ollama_models=[],
        snapshot_wrappers=[tmp_path / "memory-ledger-mv2.md"],
        honcho_env={"HONCHO_UNLOAD_EMBEDDING_MODEL_AFTER_REQUEST": "true"},
        runtime_status_writer=lambda **kwargs: writes.append(kwargs),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["divergence"]["user_missing_in_honcho_conclusions"] == []
    assert writes
    lcm_memory = writes[-1]["lcm_memory"]
    assert lcm_memory["scorecard"]["memory_sync_health"] == "ok"
    assert lcm_memory["recent_workflow_events"][-1]["details"]["missing_as_conclusions"] == []
    assert lcm_memory["recent_workflow_events"][-1]["details"]["ledger_missing_in_graphiti"] == []

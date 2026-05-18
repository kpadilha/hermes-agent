import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from agent.memory_ledger import BeliefLedger, MemoryWriteGate
from hermes_cli.memory_reconcile_cmd import (
    build_memory_reconcile_report,
    build_fix_plan,
    memory_reconcile_command,
    sync_honcho_peer_card_from_user_md,
    sync_honcho_conclusions_from_user_md,
    delete_exact_duplicate_honcho_conclusions,
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


def test_build_memory_reconcile_report_warns_on_stale_memvid_snapshot(tmp_path):
    memories = tmp_path / "memories"
    memories.mkdir()
    wrapper = tmp_path / "memory-ledger-mv2.md"
    wrapper.write_text("# snapshot wrapper", encoding="utf-8")
    old = time.time() - (8 * 24 * 60 * 60)
    os.utime(wrapper, (old, old))
    ledger = BeliefLedger(tmp_path / "ledger.db")

    report = build_memory_reconcile_report(
        memory_dir=memories,
        ledger=ledger,
        graph_facts=[],
        honcho_card=[],
        honcho_conclusions=[],
        ollama_models=[{"name": "phi4-mini:latest"}, {"name": "qwen3.5:latest"}],
        snapshot_wrappers=[wrapper],
        honcho_env={"HONCHO_UNLOAD_EMBEDDING_MODEL_AFTER_REQUEST": "true"},
    )

    assert any(r["code"] == "memvid_snapshot_stale" for r in report["recommendations"])


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


def test_discover_honcho_conclusions_follows_all_pages():
    from hermes_cli.memory_reconcile_cmd import _discover_honcho_conclusions

    calls = []

    def fake_http(method, url, payload=None, timeout=10):
        calls.append(url)
        assert method == "POST"
        assert payload == {"filters": {"observer_id": "niko", "observed_id": "96809052"}}
        if "page=1" in url:
            return {
                "items": [{"content": f"fact-{i}"} for i in range(100)],
                "page": 1,
                "size": 100,
                "pages": 2,
                "total": 101,
            }
        if "page=2" in url:
            return {
                "items": [{"content": "fact-100"}],
                "page": 2,
                "size": 100,
                "pages": 2,
                "total": 101,
            }
        raise AssertionError(url)

    conclusions = _discover_honcho_conclusions(http_json=fake_http)

    assert len(conclusions) == 101
    assert conclusions[0] == "fact-0"
    assert conclusions[-1] == "fact-100"
    assert len(calls) == 2
    assert all("size=100" in call for call in calls)


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


def test_memory_reconcile_reports_honcho_duplicate_conclusion_guardrail(tmp_path, capsys):
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "USER.md").write_text("Name: Krishna", encoding="utf-8")
    ledger = BeliefLedger(tmp_path / "ledger.db")

    memory_reconcile_command(
        SimpleNamespace(json=True, fix=False, dry_run=False, apply_action="", honcho_peer="96809052"),
        memory_dir=memories,
        ledger=ledger,
        graph_facts=[{"object": "Name: Krishna"}],
        honcho_card=["Name: Krishna"],
        honcho_conclusions=["Name: Krishna", "Name: Krishna"],
        ollama_models=[],
        snapshot_wrappers=[tmp_path / "memory-ledger-mv2.md"],
        honcho_env={"HONCHO_UNLOAD_EMBEDDING_MODEL_AFTER_REQUEST": "true"},
        runtime_status_writer=lambda **kwargs: None,
    )

    payload = json.loads(capsys.readouterr().out)
    honcho = payload["sources"]["honcho"]
    assert honcho["conclusion_count"] == 2
    assert honcho["unique_conclusion_count"] == 1
    assert honcho["duplicate_extra_count"] == 1
    assert payload["divergence"]["honcho_duplicate_conclusions"]["duplicate_extra_count"] == 1
    assert any(r["code"] == "honcho_duplicate_conclusions" for r in payload["recommendations"])


def test_memory_reconcile_lcm_memory_uses_fail_severity_not_warn_info(tmp_path):
    from hermes_cli.memory_reconcile_cmd import build_lcm_memory_state

    report = {
        "divergence": {
            "user_missing_in_honcho_card": ["warning peer-card semantic lag"],
            "user_missing_in_honcho_conclusions": ["informational semantic lag"],
            "ledger_missing_in_graphiti": ["warning projection lag"],
        },
        "recommendations": [
            {"code": "honcho_card_missing_user_entries", "severity": "warn"},
            {"code": "honcho_conclusions_missing_user_entries", "severity": "info"},
            {"code": "graphiti_projection_stale", "severity": "warn"},
        ],
    }

    lcm_memory = build_lcm_memory_state(report)

    assert lcm_memory["recent_workflow_events"][-1]["outcome"] == "success"
    assert lcm_memory["scorecard"]["memory_sync_health"] == "ok"
    assert lcm_memory["recent_workflow_events"][-1]["details"]["fail_recommendation_codes"] == []


def test_memory_reconcile_lcm_memory_fails_on_fail_recommendations(tmp_path):
    from hermes_cli.memory_reconcile_cmd import build_lcm_memory_state

    report = {
        "divergence": {
            "user_missing_in_honcho_card": [],
            "user_missing_in_honcho_conclusions": [],
            "ledger_missing_in_graphiti": [],
        },
        "recommendations": [
            {"code": "graphiti_discovery_failed", "severity": "fail"},
        ],
    }

    lcm_memory = build_lcm_memory_state(report)

    assert lcm_memory["recent_workflow_events"][-1]["outcome"] == "failure"
    assert lcm_memory["scorecard"]["memory_sync_health"] == "degraded"
    assert lcm_memory["recent_workflow_events"][-1]["details"]["fail_recommendation_codes"] == ["graphiti_discovery_failed"]


def test_memory_reconcile_surfaces_discovery_failures_as_fail_recommendations(tmp_path, monkeypatch):
    from hermes_cli import memory_reconcile_cmd as reconcile

    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "USER.md").write_text("Name: Krishna", encoding="utf-8")
    ledger = BeliefLedger(tmp_path / "ledger.db")

    monkeypatch.setattr(reconcile, "_discover_graph_facts", lambda: (_ for _ in ()).throw(RuntimeError("neo4j down")))
    monkeypatch.setattr(reconcile, "_discover_honcho_peer_card", lambda: ["Name: Krishna"])
    monkeypatch.setattr(reconcile, "_discover_honcho_conclusions", lambda: ["Name: Krishna"])
    monkeypatch.setattr(reconcile, "_discover_ollama_models", lambda: [])

    report = reconcile.build_memory_reconcile_report(
        memory_dir=memories,
        ledger=ledger,
        graph_facts=None,
        honcho_card=None,
        honcho_conclusions=None,
        ollama_models=None,
        snapshot_wrappers=[tmp_path / "memory-ledger-mv2.md"],
        honcho_env={"HONCHO_UNLOAD_EMBEDDING_MODEL_AFTER_REQUEST": "true"},
    )

    assert report["success"] is False
    assert report["discovery_errors"] == [{"source": "graphiti", "error": "neo4j down", "type": "RuntimeError"}]
    assert any(r["code"] == "graphiti_discovery_failed" and r["severity"] == "fail" for r in report["recommendations"])



def test_memory_reconcile_reports_read_only_honcho_hygiene(tmp_path):
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "USER.md").write_text("Name: Krishna\n§\nTimezone: Europe/Zurich", encoding="utf-8")
    ledger = BeliefLedger(tmp_path / "ledger.db")

    report = build_memory_reconcile_report(
        memory_dir=memories,
        ledger=ledger,
        graph_facts=[],
        honcho_card=["Name: Krishna"],
        honcho_conclusions=[
            {"id": "c1", "content": "Name: Krishna", "created_at": "2026-01-01T00:00:00Z"},
            {"id": "c2", "content": "Name: Krishna", "created_at": "2026-01-02T00:00:00Z"},
            {"id": "c3", "content": "Old fact", "created_at": "2026-01-03T00:00:00Z"},
        ],
        ollama_models=[],
        snapshot_wrappers=[tmp_path / "memory-ledger-mv2.md"],
        honcho_env={"HONCHO_UNLOAD_EMBEDDING_MODEL_AFTER_REQUEST": "true"},
    )

    hygiene = report["honcho_hygiene"]
    assert hygiene["total_conclusions"] == 3
    assert hygiene["unique_conclusions"] == 2
    assert hygiene["exact_duplicate_extra_count"] == 1
    assert hygiene["exact_duplicate_groups"][0]["keep_id"] == "c1"
    assert hygiene["exact_duplicate_groups"][0]["delete_candidate_ids"] == ["c2"]
    assert hygiene["user_entry_matches"] == ["Name: Krishna"]
    assert hygiene["user_entries_missing_as_conclusions"] == ["Timezone: Europe/Zurich"]
    assert hygiene["conclusions_not_in_user_md"] == ["Old fact"]


def test_build_fix_plan_includes_exact_honcho_duplicate_dedupe_dry_run():
    report = {
        "recommendations": [{"code": "honcho_duplicate_conclusions", "severity": "warn"}],
        "divergence": {},
        "honcho_hygiene": {
            "exact_duplicate_groups": [
                {"content": "Name: Krishna", "keep_id": "c1", "delete_candidate_ids": ["c2"]}
            ]
        },
    }

    plan = build_fix_plan(report, dry_run=True)

    action = next(a for a in plan["proposed_actions"] if a["id"] == "delete_exact_duplicate_honcho_conclusions")
    assert action["mutates"] is False
    assert action["items"] == [{"content": "Name: Krishna", "keep_id": "c1", "delete_candidate_ids": ["c2"]}]


def test_delete_exact_duplicate_honcho_conclusions_dry_run_does_not_delete():
    calls = []

    def fake_http(method, url, payload=None, timeout=10):
        calls.append((method, url, payload))
        raise AssertionError("dry run must not call HTTP")

    result = delete_exact_duplicate_honcho_conclusions(
        honcho_conclusions=[
            {"id": "c1", "content": "Name: Krishna"},
            {"id": "c2", "content": "Name: Krishna"},
        ],
        http_json=fake_http,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["would_delete_count"] == 1
    assert result["delete_candidate_ids"] == ["c2"]
    assert calls == []


def test_delete_exact_duplicate_honcho_conclusions_applies_and_validates_readback():
    remaining = [
        {"id": "c1", "content": "Name: Krishna"},
        {"id": "c2", "content": "Name: Krishna"},
    ]
    calls = []

    def fake_http(method, url, payload=None, timeout=10):
        calls.append((method, url, payload))
        if method == "DELETE" and url.endswith("/conclusions/c2"):
            remaining[:] = [item for item in remaining if item["id"] != "c2"]
            return {}
        if method == "POST" and "/conclusions/list" in url:
            return {"items": remaining, "page": 1, "pages": 1, "size": 100, "total": len(remaining)}
        raise AssertionError((method, url, payload))

    result = delete_exact_duplicate_honcho_conclusions(
        honcho_conclusions=list(remaining),
        base_url="http://honcho.test/v3",
        workspace_id="niko-main",
        http_json=fake_http,
        dry_run=False,
    )

    assert result["success"] is True
    assert result["deleted_count"] == 1
    assert result["validation"]["duplicate_extra_count_after"] == 0
    assert any(call[0] == "DELETE" for call in calls)


def test_memory_reconcile_apply_duplicate_delete_outputs_validation(tmp_path, capsys):
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "USER.md").write_text("Name: Krishna", encoding="utf-8")
    ledger = BeliefLedger(tmp_path / "ledger.db")

    def fake_delete(**kwargs):
        return {"success": True, "dry_run": True, "would_delete_count": 1, "validation": {"duplicate_extra_count_after": None}}

    memory_reconcile_command(
        SimpleNamespace(json=True, fix=False, dry_run=True, apply_action="delete_exact_duplicate_honcho_conclusions", honcho_peer="96809052"),
        memory_dir=memories,
        ledger=ledger,
        graph_facts=[],
        honcho_card=["Name: Krishna"],
        honcho_conclusions=[{"id": "c1", "content": "Name: Krishna"}, {"id": "c2", "content": "Name: Krishna"}],
        ollama_models=[],
        snapshot_wrappers=[],
        honcho_env={},
        duplicate_conclusion_deleter=fake_delete,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["apply_result"]["success"] is True
    assert payload["apply_result"]["would_delete_count"] == 1



def test_discover_graph_facts_returns_full_object_text_for_long_projection(monkeypatch):
    from hermes_cli import memory_reconcile_cmd as reconcile

    monkeypatch.setattr(reconcile.Path, "exists", lambda self: True)
    captured = {}

    class Proc:
        returncode = 0
        stdout = '[{"subject":"system","predicate":"states","object":"truncated","full_object":"full long object"}]'
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["code"] = cmd[2]
        return Proc()

    monkeypatch.setattr(reconcile.subprocess, "run", fake_run)

    facts = reconcile._discover_graph_facts()

    assert facts[0]["full_object"] == "full long object"
    assert "o.full_text" in captured["code"]
    assert "AS full_object" in captured["code"]

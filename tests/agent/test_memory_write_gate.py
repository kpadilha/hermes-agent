"""Tests for local Memory Write Gate and Belief/Evidence Ledger."""

import json

import pytest

from agent.memory_ledger import BeliefLedger, MemoryWriteGate


def test_write_gate_adds_new_fact_with_evidence(tmp_path):
    ledger = BeliefLedger(tmp_path / "memory-ledger.db")
    gate = MemoryWriteGate(ledger)

    decision = gate.evaluate_and_record(
        target="user",
        content="Krishna prefers self-hosted memory systems.",
        source="memory_tool:add:user",
        evidence_ref="memory:USER.md#add",
    )

    assert decision["operation"] == "ADD"
    assert decision["record"]["type"] == "preference"
    assert decision["record"]["confidence"] >= 0.7
    rows = ledger.search("self-hosted")
    assert len(rows) == 1
    assert rows[0]["status"] == "active"
    assert rows[0]["evidence_ref"] == "memory:USER.md#add"


def test_write_gate_noops_exact_duplicate(tmp_path):
    ledger = BeliefLedger(tmp_path / "memory-ledger.db")
    gate = MemoryWriteGate(ledger)

    first = gate.evaluate_and_record(
        target="memory",
        content="Project uses pytest.",
        source="memory_tool:add:memory",
        evidence_ref="memory:MEMORY.md#add",
    )
    second = gate.evaluate_and_record(
        target="memory",
        content="Project uses pytest.",
        source="memory_tool:add:memory",
        evidence_ref="memory:MEMORY.md#add-2",
    )

    assert first["operation"] == "ADD"
    assert second["operation"] == "NOOP"
    assert second["reason"] == "exact_duplicate"
    assert len(ledger.search("pytest")) == 1


def test_write_gate_supersedes_old_preference_for_same_subject_predicate(tmp_path):
    ledger = BeliefLedger(tmp_path / "memory-ledger.db")
    gate = MemoryWriteGate(ledger)

    old = gate.evaluate_and_record(
        target="user",
        content="Krishna prefers SaaS memory systems.",
        source="memory_tool:add:user",
        evidence_ref="memory:USER.md#old",
    )
    new = gate.evaluate_and_record(
        target="user",
        content="Krishna prefers self-hosted memory systems.",
        source="memory_tool:replace:user",
        evidence_ref="memory:USER.md#replace",
        old_content="Krishna prefers SaaS memory systems.",
    )

    assert old["operation"] == "ADD"
    assert new["operation"] == "SUPERSEDE"
    rows = ledger.search("memory systems")
    statuses = {row["object"]: row["status"] for row in rows}
    assert statuses["Krishna prefers SaaS memory systems."] == "superseded"
    assert statuses["Krishna prefers self-hosted memory systems."] == "active"
    assert new["record"]["supersedes"]


def test_ledger_audit_reports_counts_and_recent_decisions(tmp_path):
    ledger = BeliefLedger(tmp_path / "memory-ledger.db")
    gate = MemoryWriteGate(ledger)
    gate.evaluate_and_record(
        target="memory",
        content="Hermes uses LCM as context engine.",
        source="memory_tool:add:memory",
        evidence_ref="memory:MEMORY.md#lcm",
    )

    audit = ledger.audit()

    assert audit["records"]["active"] == 1
    assert audit["decisions"]["ADD"] == 1
    assert audit["recent_decisions"][0]["operation"] == "ADD"
    json.dumps(audit)  # CLI/tool payload safe


def test_write_gate_update_existing_record(tmp_path):
    ledger = BeliefLedger(tmp_path / "memory-ledger.db")
    gate = MemoryWriteGate(ledger)
    first = gate.evaluate_and_record(
        target="memory",
        content="Hermes uses LCM as context engine.",
        source="memory_tool:add:memory",
        evidence_ref="memory:MEMORY.md#add",
    )

    updated = gate.update_record(
        record_id=first["record"]["id"],
        content="Hermes uses LCM as active context engine.",
        source="cli:update",
        evidence_ref="cli:update#1",
    )

    assert updated["operation"] == "UPDATE"
    assert updated["record"]["object"] == "Hermes uses LCM as active context engine."
    assert ledger.audit()["decisions"]["UPDATE"] == 1


def test_write_gate_delete_existing_record(tmp_path):
    ledger = BeliefLedger(tmp_path / "memory-ledger.db")
    gate = MemoryWriteGate(ledger)
    first = gate.evaluate_and_record(
        target="user",
        content="Krishna prefers local memory.",
        source="test",
        evidence_ref="test#add",
    )

    deleted = gate.delete_record(
        record_id=first["record"]["id"],
        source="cli:delete",
        evidence_ref="cli:delete#1",
    )

    assert deleted["operation"] == "DELETE"
    assert ledger.get_record(first["record"]["id"])["status"] == "deleted"
    assert ledger.audit()["decisions"]["DELETE"] == 1


def test_ledger_find_active_conflicts_groups_same_subject_predicate(tmp_path):
    ledger = BeliefLedger(tmp_path / "memory-ledger.db")
    ledger.add_record({
        "type": "belief",
        "subject": "Krishna",
        "predicate": "prefers",
        "object": "Krishna prefers local memory.",
        "source": "test",
        "evidence_ref": "e1",
        "confidence": 0.6,
        "storage_targets": "user",
    })
    ledger.add_record({
        "type": "belief",
        "subject": "Krishna",
        "predicate": "prefers",
        "object": "Krishna prefers SaaS memory.",
        "source": "test",
        "evidence_ref": "e2",
        "confidence": 0.6,
        "storage_targets": "user",
    })

    conflicts = ledger.find_active_conflicts()

    assert conflicts["conflict_count"] == 1
    assert conflicts["conflicts"][0]["subject"] == "Krishna"
    assert len(conflicts["conflicts"][0]["records"]) == 2


def test_ledger_active_conflicts_ignores_independent_system_facts(tmp_path):
    ledger = BeliefLedger(tmp_path / "memory-ledger.db")
    ledger.add_record({
        "type": "fact",
        "subject": "system",
        "predicate": "uses",
        "object": "Graphiti discovery uses full_text for long facts.",
        "source": "test",
        "evidence_ref": "e1",
        "confidence": 0.7,
        "storage_targets": "memory",
    })
    ledger.add_record({
        "type": "fact",
        "subject": "system",
        "predicate": "uses",
        "object": "Content Skill Graph uses wikilinks for editorial profiles.",
        "source": "test",
        "evidence_ref": "e2",
        "confidence": 0.7,
        "storage_targets": "memory",
    })

    conflicts = ledger.find_active_conflicts()

    assert conflicts["conflict_count"] == 0


def test_write_gate_does_not_supersede_unrelated_system_facts_with_generic_predicate(tmp_path):
    ledger = BeliefLedger(tmp_path / "memory-ledger.db")
    gate = MemoryWriteGate(ledger)

    first = gate.evaluate_and_record(
        target="memory",
        content="Graphiti discovery uses full_text for long facts.",
        source="memory_tool:add:memory",
        evidence_ref="memory:MEMORY.md#graphiti",
    )
    second = gate.evaluate_and_record(
        target="memory",
        content="Content Skill Graph uses wikilinks for editorial profiles.",
        source="memory_tool:add:memory",
        evidence_ref="memory:MEMORY.md#content",
    )

    assert first["operation"] == "ADD"
    assert second["operation"] == "ADD"
    rows = ledger.list_records(status="active")
    assert {row["object"] for row in rows} == {
        "Graphiti discovery uses full_text for long facts.",
        "Content Skill Graph uses wikilinks for editorial profiles.",
    }


def test_ledger_list_records_returns_all_records_without_search_cap(tmp_path):
    ledger = BeliefLedger(tmp_path / "memory-ledger.db")
    for i in range(25):
        ledger.add_record({
            "type": "fact",
            "subject": "Record",
            "predicate": "states",
            "object": f"record-{i}",
            "source": "test",
            "evidence_ref": f"test#{i}",
            "storage_targets": "memory",
            "status": "superseded" if i % 2 else "active",
        })

    assert len(ledger.search("", limit=10)) == 10
    assert len(ledger.list_records()) == 25
    superseded = ledger.list_records(status="superseded")
    assert len(superseded) == 12
    assert {row["status"] for row in superseded} == {"superseded"}


def test_ledger_mutations_fail_loudly_on_missing_or_inactive_records(tmp_path):
    ledger = BeliefLedger(tmp_path / "memory-ledger.db")
    record = ledger.add_record({
        "type": "fact",
        "subject": "system",
        "predicate": "states",
        "object": "record exists",
        "source": "test",
        "evidence_ref": "test#add",
        "storage_targets": "memory",
    })

    with pytest.raises(ValueError, match="update_record affected 0 rows"):
        ledger.update_record(9999, content="missing", source="test", evidence_ref="test#missing")

    ledger.mark_deleted(record["id"], evidence_ref="test#delete")

    with pytest.raises(ValueError, match="update_record affected 0 rows"):
        ledger.update_record(record["id"], content="stale", source="test", evidence_ref="test#stale")
    with pytest.raises(ValueError, match="mark_deleted affected 0 rows"):
        ledger.mark_deleted(record["id"], evidence_ref="test#delete-again")
    with pytest.raises(ValueError, match="touch_record affected 0 rows"):
        ledger.touch_record(record["id"])

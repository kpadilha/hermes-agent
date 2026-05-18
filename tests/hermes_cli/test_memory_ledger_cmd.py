"""Tests for `hermes memory ledger ...` command helpers."""

import json
from types import SimpleNamespace

from agent.memory_ledger import BeliefLedger, MemoryWriteGate
from hermes_cli.memory_ledger_cmd import memory_ledger_command


def test_memory_ledger_audit_prints_json(tmp_path, capsys):
    ledger = BeliefLedger(tmp_path / "ledger.db")
    gate = MemoryWriteGate(ledger)
    gate.evaluate_and_record(
        target="user",
        content="Krishna prefers self-hosted memory systems.",
        source="test",
        evidence_ref="test#1",
    )

    memory_ledger_command(SimpleNamespace(ledger_command="audit", json=True), ledger=ledger)

    payload = json.loads(capsys.readouterr().out)
    assert payload["records"]["active"] == 1
    assert payload["decisions"]["ADD"] == 1


def test_memory_ledger_search_prints_matching_records(tmp_path, capsys):
    ledger = BeliefLedger(tmp_path / "ledger.db")
    gate = MemoryWriteGate(ledger)
    gate.evaluate_and_record(
        target="memory",
        content="Hermes uses LCM as context engine.",
        source="test",
        evidence_ref="test#2",
    )

    memory_ledger_command(
        SimpleNamespace(ledger_command="search", query="LCM", limit=5, json=False),
        ledger=ledger,
    )

    out = capsys.readouterr().out
    assert "Hermes uses LCM as context engine." in out
    assert "active" in out


def test_memory_ledger_add_uses_write_gate(tmp_path, capsys):
    ledger = BeliefLedger(tmp_path / "ledger.db")

    memory_ledger_command(
        SimpleNamespace(
            ledger_command="add",
            target="user",
            content="Krishna prefers local-first memory.",
            source="cli-test",
            evidence_ref="cli-test#add",
            json=True,
        ),
        ledger=ledger,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "ADD"
    assert ledger.search("local-first")[0]["type"] == "preference"


def test_memory_ledger_update_uses_write_gate(tmp_path, capsys):
    ledger = BeliefLedger(tmp_path / "ledger.db")
    gate = MemoryWriteGate(ledger)
    added = gate.evaluate_and_record(
        target="memory",
        content="Hermes uses LCM.",
        source="test",
        evidence_ref="old",
    )

    memory_ledger_command(
        SimpleNamespace(
            ledger_command="update",
            record_id=added["record"]["id"],
            content="Hermes uses LCM as active context engine.",
            source="cli-test",
            evidence_ref="cli-test#update",
            json=True,
        ),
        ledger=ledger,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "UPDATE"
    assert ledger.get_record(added["record"]["id"])["object"] == "Hermes uses LCM as active context engine."


def test_memory_ledger_delete_uses_write_gate(tmp_path, capsys):
    ledger = BeliefLedger(tmp_path / "ledger.db")
    gate = MemoryWriteGate(ledger)
    added = gate.evaluate_and_record(
        target="memory",
        content="Temporary memory fact.",
        source="test",
        evidence_ref="old",
    )

    memory_ledger_command(
        SimpleNamespace(
            ledger_command="delete",
            record_id=added["record"]["id"],
            source="cli-test",
            evidence_ref="cli-test#delete",
            json=True,
        ),
        ledger=ledger,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "DELETE"
    assert ledger.get_record(added["record"]["id"])["status"] == "deleted"


def test_memory_ledger_promote_prints_markdown(tmp_path, capsys):
    ledger = BeliefLedger(tmp_path / "ledger.db")
    gate = MemoryWriteGate(ledger)
    added = gate.evaluate_and_record(
        target="user",
        content="Krishna prefers self-hosted memory systems.",
        source="test",
        evidence_ref="evidence#1",
    )

    memory_ledger_command(
        SimpleNamespace(ledger_command="promote", record_id=added["record"]["id"], json=False),
        ledger=ledger,
    )

    out = capsys.readouterr().out
    assert "Krishna prefers self-hosted memory systems." in out
    assert "evidence#1" in out
    assert "Memory Ledger Promotion Candidate" in out


def test_memory_ledger_contradictions_reports_superseded_records(tmp_path, capsys):
    ledger = BeliefLedger(tmp_path / "ledger.db")
    gate = MemoryWriteGate(ledger)
    gate.evaluate_and_record(
        target="user",
        content="Krishna prefers SaaS memory systems.",
        source="test",
        evidence_ref="old",
    )
    gate.evaluate_and_record(
        target="user",
        content="Krishna prefers self-hosted memory systems.",
        old_content="Krishna prefers SaaS memory systems.",
        source="test",
        evidence_ref="new",
    )

    memory_ledger_command(
        SimpleNamespace(ledger_command="contradictions", json=True),
        ledger=ledger,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["superseded_count"] == 1
    assert payload["superseded_records"][0]["status"] == "superseded"


def test_memory_ledger_export_writes_markdown_projection(tmp_path, capsys):
    ledger = BeliefLedger(tmp_path / "ledger.db")
    gate = MemoryWriteGate(ledger)
    gate.evaluate_and_record(
        target="user",
        content="Krishna prefers self-hosted memory systems.",
        source="test",
        evidence_ref="evidence#1",
    )
    out = tmp_path / "ledger-export.md"

    memory_ledger_command(
        SimpleNamespace(ledger_command="export", format="markdown", output=str(out), json=True),
        ledger=ledger,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["output"] == str(out)
    text = out.read_text(encoding="utf-8")
    assert "# Memory Ledger Projection" in text
    assert "Krishna prefers self-hosted memory systems." in text
    assert "evidence#1" in text


def test_memory_ledger_export_writes_json_projection(tmp_path, capsys):
    ledger = BeliefLedger(tmp_path / "ledger.db")
    gate = MemoryWriteGate(ledger)
    gate.evaluate_and_record(
        target="memory",
        content="Hermes uses LCM.",
        source="test",
        evidence_ref="evidence#2",
    )
    out = tmp_path / "ledger-export.json"

    memory_ledger_command(
        SimpleNamespace(ledger_command="export", format="json", output=str(out), json=True),
        ledger=ledger,
    )

    payload = json.loads(capsys.readouterr().out)
    exported = json.loads(out.read_text(encoding="utf-8"))
    assert payload["success"] is True
    assert exported["records"][0]["object"] == "Hermes uses LCM."


def test_memory_ledger_json_export_can_auto_write_markdown_wrapper(tmp_path, capsys):
    ledger = BeliefLedger(tmp_path / "ledger.db")
    gate = MemoryWriteGate(ledger)
    gate.evaluate_and_record(
        target="user",
        content="Krishna prefers self-hosted memory systems.",
        source="test",
        evidence_ref="evidence#3",
    )
    out = tmp_path / "ledger-export.json"

    memory_ledger_command(
        SimpleNamespace(
            ledger_command="export",
            format="json",
            output=str(out),
            markdown_wrapper=True,
            json=True,
        ),
        ledger=ledger,
    )

    payload = json.loads(capsys.readouterr().out)
    wrapper = tmp_path / "ledger-export-json.md"
    assert payload["success"] is True
    assert payload["markdown_wrapper"] == str(wrapper)
    assert wrapper.exists()
    wrapper_text = wrapper.read_text(encoding="utf-8")
    assert "# Memory Ledger Projection JSON" in wrapper_text
    assert "```json" in wrapper_text
    assert "Krishna prefers self-hosted memory systems." in wrapper_text

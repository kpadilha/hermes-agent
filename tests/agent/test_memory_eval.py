"""Tests for deterministic local Krishna MemoryEval."""

from pathlib import Path

from agent.memory_eval import KrishnaMemoryEval
from agent.memory_ledger import BeliefLedger


def test_memory_eval_scores_local_surfaces(tmp_path):
    hermes_home = tmp_path / ".hermes"
    memories = hermes_home / "memories"
    memories.mkdir(parents=True)
    (memories / "USER.md").write_text(
        "Krishna prefers self-hosted/local-first memory and infrastructure.\n§\n"
        "User prefers idle reset mode for Telegram DMs with 4320 min.\n",
        encoding="utf-8",
    )
    (memories / "MEMORY.md").write_text(
        "USER.md is the authoritative user profile source; Honcho peer card is projection.\n§\n"
        "LCM is active as context engine and proves memory/runtime state.\n",
        encoding="utf-8",
    )
    kb_root = tmp_path / "kb"
    note = kb_root / "wiki" / "operations" / "hermes-memory-architecture.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "Memory Write Gate and Belief/Evidence Ledger are local-first.\n"
        "Heavy research lives in KB / Obsidian as Compiled Truth.\n",
        encoding="utf-8",
    )

    evaluator = KrishnaMemoryEval(hermes_home=hermes_home, kb_root=kb_root)
    result = evaluator.run()

    assert result["summary"]["total"] >= 6
    assert result["summary"]["passed"] >= 5
    assert result["summary"]["score_pct"] >= 80
    assert any(check["id"] == "self_hosted_preference" and check["passed"] for check in result["checks"])


def test_memory_eval_reports_failures_when_sources_missing(tmp_path):
    evaluator = KrishnaMemoryEval(hermes_home=tmp_path / "missing", kb_root=tmp_path / "missing-kb")

    result = evaluator.run()

    assert result["summary"]["failed"] > 0
    assert result["summary"]["score_pct"] < 100
    assert all("id" in check and "passed" in check for check in result["checks"])


def test_memory_eval_fails_on_active_ledger_conflicts(tmp_path):
    hermes_home = tmp_path / ".hermes"
    memories = hermes_home / "memories"
    memories.mkdir(parents=True)
    (memories / "USER.md").write_text(
        "Krishna prefers self-hosted/local-first memory and infrastructure. Telegram idle reset.\n",
        encoding="utf-8",
    )
    (memories / "MEMORY.md").write_text(
        "USER.md source; Honcho projection. LCM active.\n",
        encoding="utf-8",
    )
    kb_root = tmp_path / "kb"
    note = kb_root / "wiki" / "operations" / "hermes-memory-architecture.md"
    note.parent.mkdir(parents=True)
    note.write_text("KB Compiled Truth. Memory Write Gate and Belief Ledger.\n", encoding="utf-8")
    ledger = BeliefLedger(hermes_home / "memory-ledger.db")
    ledger.add_record({"type": "belief", "subject": "Krishna", "predicate": "prefers", "object": "A", "source": "t", "evidence_ref": "e1"})
    ledger.add_record({"type": "belief", "subject": "Krishna", "predicate": "prefers", "object": "B", "source": "t", "evidence_ref": "e2"})

    result = KrishnaMemoryEval(hermes_home=hermes_home, kb_root=kb_root).run()

    conflict_check = next(check for check in result["checks"] if check["id"] == "ledger_active_conflicts")
    assert conflict_check["passed"] is False

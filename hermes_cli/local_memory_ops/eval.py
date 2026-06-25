"""Deterministic local memory evaluation for Krishna's Hermes profile.

This is not a benchmark against an LLM. It is a cheap local acceptance suite that
checks whether canonical memory surfaces contain the invariants Krishna relies
on: self-hosted preference, Telegram continuity, profile source-of-truth, KB as
compiled truth, LCM activation, and the new write gate/ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from hermes_constants import get_hermes_home
from hermes_cli.local_memory_ops.paths import default_kb_root
from hermes_cli.local_memory_ops.ledger import BeliefLedger


@dataclass(frozen=True)
class MemoryEvalCheck:
    id: str
    question: str
    surface: str
    path: Path
    must_include_any: tuple[str, ...]
    must_include_all: tuple[str, ...] = ()


class KrishnaMemoryEval:
    """Run deterministic checks over local memory surfaces."""

    def __init__(self, *, hermes_home: Optional[Path] = None, kb_root: Optional[Path] = None) -> None:
        self.hermes_home = Path(hermes_home) if hermes_home else get_hermes_home()
        self.kb_root = Path(kb_root) if kb_root else default_kb_root()

    def checks(self) -> List[MemoryEvalCheck]:
        return [
            MemoryEvalCheck(
                id="self_hosted_preference",
                question="Does the user profile preserve Krishna's self-hosted/local-first memory preference?",
                surface="USER.md",
                path=self.hermes_home / "memories" / "USER.md",
                must_include_any=("self-hosted", "local-first"),
                must_include_all=("Krishna",),
            ),
            MemoryEvalCheck(
                id="telegram_idle_reset",
                question="Does memory preserve the Telegram DM idle reset policy?",
                surface="USER.md",
                path=self.hermes_home / "memories" / "USER.md",
                must_include_any=("idle reset", "idle"),
                must_include_all=("Telegram",),
            ),
            MemoryEvalCheck(
                id="profile_source_of_truth",
                question="Does the memory architecture note preserve USER.md as authoritative profile source and Honcho as projection?",
                surface="KB hermes-memory-architecture",
                path=self.kb_root / "wiki" / "operations" / "hermes-memory-architecture.md",
                must_include_any=("USER.md",),
                must_include_all=("Honcho", "projection"),
            ),
            MemoryEvalCheck(
                id="lcm_active",
                question="Does memory preserve that LCM is active/proof layer?",
                surface="MEMORY.md",
                path=self.hermes_home / "memories" / "MEMORY.md",
                must_include_any=("LCM", "lcm"),
                must_include_all=(),
            ),
            MemoryEvalCheck(
                id="kb_compiled_truth",
                question="Does the KB memory architecture note preserve KB/Obsidian as Compiled Truth?",
                surface="KB hermes-memory-architecture",
                path=self.kb_root / "wiki" / "operations" / "hermes-memory-architecture.md",
                must_include_any=("Compiled Truth", "compiled truth", "KB / Obsidian", "Obsidian"),
                must_include_all=("KB",),
            ),
            MemoryEvalCheck(
                id="write_gate_ledger",
                question="Does the KB preserve the Memory Write Gate / Belief Ledger decision?",
                surface="KB hermes-memory-architecture",
                path=self.kb_root / "wiki" / "operations" / "hermes-memory-architecture.md",
                must_include_any=("Memory Write Gate",),
                must_include_all=("Belief", "Ledger"),
            ),
        ]

    def run(self) -> Dict[str, object]:
        results = [self._run_check(check) for check in self.checks()]
        results.append(self._run_ledger_conflict_check())
        passed = sum(1 for item in results if item["passed"])
        total = len(results)
        failed = total - passed
        return {
            "summary": {
                "passed": passed,
                "failed": failed,
                "total": total,
                "score_pct": round((passed / total) * 100, 1) if total else 0.0,
            },
            "checks": results,
        }

    def _run_check(self, check: MemoryEvalCheck) -> Dict[str, object]:
        text = self._read(check.path)
        missing_all = [needle for needle in check.must_include_all if needle not in text]
        any_match = any(needle in text for needle in check.must_include_any)
        passed = bool(text) and any_match and not missing_all
        return {
            "id": check.id,
            "question": check.question,
            "surface": check.surface,
            "path": str(check.path),
            "passed": passed,
            "matched_any": [needle for needle in check.must_include_any if needle in text],
            "missing_all": missing_all,
            "missing_any_group": [] if any_match else list(check.must_include_any),
        }

    def _run_ledger_conflict_check(self) -> Dict[str, object]:
        ledger_path = self.hermes_home / "memory-ledger.db"
        if not ledger_path.exists():
            return {
                "id": "ledger_active_conflicts",
                "question": "Does the structured ledger avoid unresolved active conflicts?",
                "surface": "memory-ledger.db",
                "path": str(ledger_path),
                "passed": True,
                "conflict_count": 0,
                "note": "ledger does not exist yet",
            }
        conflicts = BeliefLedger(ledger_path).find_active_conflicts()
        return {
            "id": "ledger_active_conflicts",
            "question": "Does the structured ledger avoid unresolved active conflicts?",
            "surface": "memory-ledger.db",
            "path": str(ledger_path),
            "passed": conflicts["conflict_count"] == 0,
            "conflict_count": conflicts["conflict_count"],
            "conflicts": conflicts["conflicts"],
        }

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

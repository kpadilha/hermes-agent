#!/usr/bin/env python3
"""Static guardrails for silent operational bugs.

This is intentionally narrow and root-cause oriented. It guards against bug
classes that caused real operational failures:
- partial projection reads hidden behind fixed limits;
- paginated Honcho conclusions read as a single page;
- failed local memory writes leaking into semantic memory projections.

The script exits non-zero only for high-confidence findings. It is not a generic
lint tool and should stay small/noise-free.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Finding:
    code: str
    severity: str
    path: str
    line: int
    detail: str
    snippet: str = ""


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _line_of(text: str, needle: str) -> tuple[int, str]:
    for idx, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return idx, line.strip()
    return 1, ""


def audit_repo(repo: Path) -> list[Finding]:
    findings: list[Finding] = []

    reconcile = repo / "hermes_cli" / "memory_reconcile_cmd.py"
    reconcile_text = _read(reconcile)
    rel = str(reconcile.relative_to(repo))
    if "conclusions/list?size={size}&page={page}" not in reconcile_text or "while True:" not in reconcile_text:
        line, snippet = _line_of(reconcile_text, "conclusions/list")
        findings.append(Finding(
            code="honcho_conclusions_not_paginated",
            severity="fail",
            path=rel,
            line=line,
            detail="Honcho conclusions must be read page-by-page; a single page silently hides facts after 100 conclusions.",
            snippet=snippet,
        ))

    silent_discovery_pattern = re.compile(r"except[^\n]*Exception[^\n]*:\n[ \t]+return[ \t]+(?:\[\]|\{\})")
    if silent_discovery_pattern.search(reconcile_text):
        line, snippet = _line_of(reconcile_text, 'except Exception:')
        findings.append(Finding(
            code="memory_reconcile_silent_discovery_failure",
            severity="fail",
            path=rel,
            line=line,
            detail="Memory reconcile discovery failures must surface as structured discovery_errors/recommendations, not empty data.",
            snippet=snippet,
        ))

    run_agent = repo / "run_agent.py"
    run_text = _read(run_agent)
    rel = str(run_agent.relative_to(repo))
    for match in re.finditer(r"on_memory_write\(", run_text):
        prefix = run_text[max(0, match.start() - 450):match.start()]
        line = run_text[:match.start()].count("\n") + 1
        snippet = run_text.splitlines()[line - 1].strip() if line - 1 < len(run_text.splitlines()) else ""
        if 'get("success") is True' not in prefix and "get('success') is True" not in prefix:
            findings.append(Finding(
                code="memory_projection_not_success_gated",
                severity="fail",
                path=rel,
                line=line,
                detail="External memory providers must be notified only after the built-in memory write succeeds.",
                snippet=snippet,
            ))

    ledger = repo / "agent" / "memory_ledger.py"
    ledger_text = _read(ledger)
    if "def _require_row_updated" not in ledger_text or "cursor.rowcount" not in ledger_text:
        line, snippet = _line_of(ledger_text, "def update_record")
        findings.append(Finding(
            code="ledger_mutations_not_rowcount_checked",
            severity="fail",
            path=str(ledger.relative_to(repo)),
            line=line,
            detail="Ledger UPDATE paths must verify rowcount; SQLite no-op updates otherwise look successful.",
            snippet=snippet,
        ))

    finite_all_record_pattern = re.compile(r"ledger\.search\(\s*['\"]['\"]\s*,\s*limit\s*=")
    for candidate in [
        repo / "hermes_cli" / "memory_reconcile_cmd.py",
        repo / "hermes_cli" / "memory_graph_cmd.py",
        repo / "hermes_cli" / "memory_snapshot_cmd.py",
        repo / "hermes_cli" / "memory_ledger_cmd.py",
    ]:
        text = _read(candidate)
        for match in finite_all_record_pattern.finditer(text):
            line = text[:match.start()].count("\n") + 1
            snippet = text.splitlines()[line - 1].strip()
            findings.append(Finding(
                code="ledger_all_records_fixed_limit",
                severity="fail",
                path=str(candidate.relative_to(repo)),
                line=line,
                detail="Use ledger.list_records() for all-record projections; search('', limit=N) silently truncates after N records.",
                snippet=snippet,
            ))

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Hermes operational code for known silent-bug classes.")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    repo = Path(args.repo).expanduser().resolve()
    findings = audit_repo(repo)
    payload = {
        "success": not any(f.severity == "fail" for f in findings),
        "repo": str(repo),
        "finding_count": len(findings),
        "findings": [asdict(f) for f in findings],
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Operational silent-bug audit: {'OK' if payload['success'] else 'FAIL'} ({len(findings)} findings)")
        for f in findings:
            print(f"- [{f.severity}] {f.code} {f.path}:{f.line} — {f.detail}")
            if f.snippet:
                print(f"  {f.snippet}")
    return 0 if payload["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

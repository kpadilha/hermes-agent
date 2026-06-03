#!/usr/bin/env python3
"""Evaluate deterministic context relevance pinning against fixture cases.

This is an offline, dependency-free smoke/eval helper for the default-off
relevance-pinning MVP. It does not call an LLM and does not mutate runtime state.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

# Allow running as `python scripts/eval_context_relevance.py` from repo root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.context_relevance import select_relevant_context_pins  # noqa: E402


DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "context_relevance_cases.json"


def _render_pins(pins: list[Any]) -> str:
    return "\n".join(pin.excerpt for pin in pins)


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    messages = case["messages"]
    pins = select_relevant_context_pins(
        messages,
        candidate_start=int(case.get("candidate_start", 0)),
        candidate_end=int(case.get("candidate_end", len(messages))),
        focus_topic=case.get("focus_topic"),
        max_pins=int(case.get("max_pins", 8)),
        max_chars_total=int(case.get("max_chars_total", 12000)),
        min_score=int(case.get("min_score", 3)),
    )
    rendered = _render_pins(pins)
    missing = [s for s in case.get("expected_substrings", []) if s not in rendered]
    unexpected = [s for s in case.get("unexpected_substrings", []) if s in rendered]
    return {
        "name": case.get("name", "<unnamed>"),
        "ok": not missing and not unexpected,
        "pin_count": len(pins),
        "total_chars": sum(len(pin.excerpt) for pin in pins),
        "missing_expected": missing,
        "unexpected_present": unexpected,
        "pins": [pin.__dict__ for pin in pins],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--json", action="store_true", help="Emit full JSON results")
    args = parser.parse_args()

    cases = json.loads(args.fixture.read_text(encoding="utf-8"))
    results = [evaluate_case(case) for case in cases]
    ok = all(result["ok"] for result in results)

    if args.json:
        print(json.dumps({"ok": ok, "results": results}, indent=2, ensure_ascii=False))
    else:
        for result in results:
            status = "PASS" if result["ok"] else "FAIL"
            print(
                f"{status} {result['name']} "
                f"pins={result['pin_count']} chars={result['total_chars']}"
            )
            if result["missing_expected"]:
                print(f"  missing expected: {result['missing_expected']}")
            if result["unexpected_present"]:
                print(f"  unexpected present: {result['unexpected_present']}")
        print(f"overall={'PASS' if ok else 'FAIL'} cases={len(results)}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI helper for `hermes memory eval`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from hermes_cli.local_memory_ops.eval import KrishnaMemoryEval


def memory_eval_command(args, *, hermes_home: Optional[Path] = None, kb_root: Optional[Path] = None) -> None:
    result = KrishnaMemoryEval(hermes_home=hermes_home, kb_root=kb_root).run()
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    summary = result["summary"]
    print(
        f"Krishna MemoryEval: {summary['passed']}/{summary['total']} passed "
        f"({summary['score_pct']}%)"
    )
    for check in result["checks"]:
        mark = "✓" if check["passed"] else "✗"
        print(f"  {mark} {check['id']} — {check['surface']}")
        if not check["passed"]:
            print(f"    missing_any_group: {check.get('missing_any_group', [])}")
            print(f"    missing_all: {check.get('missing_all', [])}")

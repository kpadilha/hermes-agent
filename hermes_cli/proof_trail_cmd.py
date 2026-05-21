"""Task/LCM proof trail command.

Creates compact, durable proof artifacts for important autonomous actions.
The artifacts live in Krishna's Obsidian operational workspace and are designed
for later LCM/proof-layer integration without over-engineering the current base.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_PROOF_DIR = Path.home() / "obsidian-vault" / "Krishna" / "niko" / "operations" / "proofs"


def slugify_title(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").strip().lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug or "proof"


def _list_lines(items: Iterable[str], *, code: bool = False) -> str:
    values = [str(item).strip() for item in items if str(item).strip()]
    if not values:
        return "- None recorded\n"
    if code:
        return "".join(f"- `{item}`\n" for item in values)
    return "".join(f"- {item}\n" for item in values)


def _command_blocks(commands: Iterable[str]) -> str:
    values = [str(item).strip() for item in commands if str(item).strip()]
    if not values:
        return "- None recorded\n"
    return "\n".join(f"```bash\n{cmd}\n```" for cmd in values) + "\n"


def build_proof_markdown(
    *,
    title: str,
    status: str,
    rationale: str,
    inputs: list[str],
    files: list[str],
    commands: list[str],
    validations: list[str],
    kb_promotions: list[str],
    final_state: str,
    lcm_refs: list[str],
    timestamp: str,
) -> str:
    safe_title = title.strip() or "Task Proof"
    safe_status = status.strip() or "recorded"
    return f"""---
tipo: "proof"
status: "{safe_status}"
confianca: "alta"
tags: [proof, operations]
fonte: "Niko task/LCM proof trail"
criado: "{timestamp[:10]}"
atualizado: "{timestamp[:10]}"
relacionados: ["[[hermes-memory-architecture]]"]
---

# {safe_title}

## Summary

- **Status:** {safe_status}
- **Timestamp:** {timestamp}

## Rationale

{rationale.strip() or "No rationale recorded."}

## Inputs

{_list_lines(inputs)}
## Files Changed

{_list_lines(files, code=True)}
## Commands / Evidence

{_command_blocks(commands)}
## Validation

{_list_lines(validations)}
## KB Promotion

{_list_lines(kb_promotions)}
## LCM / Context References

{_list_lines(lcm_refs)}
## Final State

{final_state.strip() or "No final state recorded."}

## Histórico

- **{timestamp[:10]}** — Proof artifact created for autonomous task execution.
"""


def _load_index(index_path: Path) -> dict[str, Any]:
    if not index_path.exists():
        return {"proofs": []}
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("proofs"), list):
            return payload
    except Exception:
        pass
    return {"proofs": []}


def create_proof_record(
    *,
    title: str,
    status: str,
    rationale: str,
    inputs: list[str],
    files: list[str],
    commands: list[str],
    validations: list[str],
    kb_promotions: list[str],
    final_state: str,
    lcm_refs: list[str],
    output_dir: Path = DEFAULT_PROOF_DIR,
    timestamp: str | None = None,
) -> dict[str, Any]:
    timestamp = timestamp or datetime.now(timezone.utc).isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)
    date_prefix = timestamp[:10]
    slug = slugify_title(title)
    proof_path = output_dir / f"{date_prefix}-{slug}.md"
    markdown = build_proof_markdown(
        title=title,
        status=status,
        rationale=rationale,
        inputs=inputs,
        files=files,
        commands=commands,
        validations=validations,
        kb_promotions=kb_promotions,
        final_state=final_state,
        lcm_refs=lcm_refs,
        timestamp=timestamp,
    )
    proof_path.write_text(markdown, encoding="utf-8")

    index_path = output_dir / "proof-index.json"
    index = _load_index(index_path)
    record = {
        "title": title,
        "status": status,
        "timestamp": timestamp,
        "path": str(proof_path),
        "slug": slug,
    }
    index["proofs"] = [p for p in index.get("proofs", []) if p.get("path") != str(proof_path)]
    index["proofs"].insert(0, record)
    index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")

    return {"success": True, "path": str(proof_path), "index_path": str(index_path), "record": record}


def proof_trail_command(args, *, timestamp: str | None = None) -> None:
    result = create_proof_record(
        title=args.title,
        status=args.status,
        rationale=args.rationale,
        inputs=list(args.inputs or []),
        files=list(args.files or []),
        commands=list(args.commands or []),
        validations=list(args.validations or []),
        kb_promotions=list(args.kb_promotions or []),
        final_state=args.final_state,
        lcm_refs=list(args.lcm_refs or []),
        output_dir=Path(args.output_dir) if args.output_dir else DEFAULT_PROOF_DIR,
        timestamp=timestamp,
    )
    if bool(getattr(args, "json", False)):
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Proof written: {result['path']}")

"""CLI helpers for `hermes memory ledger ...`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from agent.memory_ledger import BeliefLedger, MemoryWriteGate


def _emit(payload: Dict[str, Any], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if "records" in payload and "decisions" in payload:
        print(f"Ledger: {payload.get('db_path')}")
        print(f"Records: {payload.get('records', {})}")
        print(f"Decisions: {payload.get('decisions', {})}")
        return
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _print_records(records: list[Dict[str, Any]], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps({"records": records}, indent=2, ensure_ascii=False))
        return
    if not records:
        print("No records found.")
        return
    for row in records:
        print(f"[{row.get('id')}] {row.get('status')} {row.get('type')} {row.get('subject')}.{row.get('predicate')}")
        print(f"  {row.get('object')}")
        print(f"  evidence: {row.get('evidence_ref')}")


def _markdown_json_wrapper_path(json_path: Path) -> Path:
    if json_path.suffix == ".json":
        return json_path.with_name(f"{json_path.stem}-json.md")
    return json_path.with_suffix(json_path.suffix + ".md")


def _write_json_markdown_wrapper(json_path: Path, payload: Dict[str, Any]) -> Path:
    wrapper = _markdown_json_wrapper_path(json_path)
    records = payload.get("records") or []
    active_conflicts = payload.get("active_conflicts") or {}
    lines = [
        "# Memory Ledger Projection JSON",
        "",
        "This is the Obsidian-syncable Markdown wrapper for the local JSON projection.",
        "",
        f"- Source JSON: `{json_path}`",
        f"- Records: `{len(records)}`",
        f"- Active conflicts: `{active_conflicts.get('conflict_count', 0)}`",
        "",
        "```json",
        json.dumps(payload, indent=2, ensure_ascii=False),
        "```",
        "",
    ]
    wrapper.write_text("\n".join(lines), encoding="utf-8")
    return wrapper


def memory_ledger_command(args, *, ledger: Optional[BeliefLedger] = None) -> None:
    """Dispatch memory ledger subcommands.

    Args object fields are intentionally simple so this helper is easy to test
    without invoking argparse.
    """
    ledger = ledger or BeliefLedger()
    cmd = getattr(args, "ledger_command", None)
    as_json = bool(getattr(args, "json", False))

    if cmd == "audit":
        _emit(ledger.audit(), as_json=as_json)
        return

    if cmd == "search":
        query = getattr(args, "query", "") or ""
        limit = int(getattr(args, "limit", 20) or 20)
        _print_records(ledger.search(query, limit=limit), as_json=as_json)
        return

    if cmd == "add":
        gate = MemoryWriteGate(ledger)
        decision = gate.evaluate_and_record(
            target=getattr(args, "target", "memory") or "memory",
            content=getattr(args, "content", "") or "",
            source=getattr(args, "source", "cli:memory-ledger:add") or "cli:memory-ledger:add",
            evidence_ref=getattr(args, "evidence_ref", "cli:memory-ledger:add") or "cli:memory-ledger:add",
        )
        _emit(decision, as_json=as_json)
        return

    if cmd == "update":
        gate = MemoryWriteGate(ledger)
        decision = gate.update_record(
            record_id=int(getattr(args, "record_id")),
            content=getattr(args, "content", "") or "",
            source=getattr(args, "source", "cli:memory-ledger:update") or "cli:memory-ledger:update",
            evidence_ref=getattr(args, "evidence_ref", "cli:memory-ledger:update") or "cli:memory-ledger:update",
        )
        _emit(decision, as_json=as_json)
        return

    if cmd == "delete":
        gate = MemoryWriteGate(ledger)
        decision = gate.delete_record(
            record_id=int(getattr(args, "record_id")),
            source=getattr(args, "source", "cli:memory-ledger:delete") or "cli:memory-ledger:delete",
            evidence_ref=getattr(args, "evidence_ref", "cli:memory-ledger:delete") or "cli:memory-ledger:delete",
        )
        _emit(decision, as_json=as_json)
        return

    if cmd == "promote":
        record = ledger.get_record(int(getattr(args, "record_id")))
        if as_json:
            _emit({"promotion_candidate": record}, as_json=True)
            return
        print("# Memory Ledger Promotion Candidate")
        print(f"- ID: {record.get('id')}")
        print(f"- Type: {record.get('type')}")
        print(f"- Status: {record.get('status')}")
        print(f"- Subject: {record.get('subject')}")
        print(f"- Predicate: {record.get('predicate')}")
        print(f"- Evidence: {record.get('evidence_ref')}")
        print("\n## Content")
        print(record.get("object", ""))
        return

    if cmd == "export":
        fmt = getattr(args, "format", "markdown") or "markdown"
        output = Path(getattr(args, "output", "") or "memory-ledger-export.md")
        output.parent.mkdir(parents=True, exist_ok=True)
        records = ledger.list_records()
        payload = {
            "ledger": ledger.audit(),
            "records": records,
            "active_conflicts": ledger.find_active_conflicts(),
        }
        if fmt == "json":
            output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            wrapper_path = None
            if bool(getattr(args, "markdown_wrapper", False)):
                wrapper_path = _write_json_markdown_wrapper(output, payload)
        else:
            wrapper_path = None
            lines = ["# Memory Ledger Projection", ""]
            lines.append(f"- DB: `{ledger.db_path}`")
            lines.append(f"- Records: `{len(records)}`")
            lines.append(f"- Active conflicts: `{payload['active_conflicts']['conflict_count']}`")
            lines.append("")
            for row in records:
                lines.append(f"## [{row.get('id')}] {row.get('subject')}.{row.get('predicate')} — {row.get('status')}")
                lines.append(f"- Type: `{row.get('type')}`")
                lines.append(f"- Confidence: `{row.get('confidence')}`")
                lines.append(f"- Evidence: `{row.get('evidence_ref')}`")
                lines.append(f"- Source: `{row.get('source')}`")
                lines.append("")
                lines.append(str(row.get("object") or ""))
                lines.append("")
            output.write_text("\n".join(lines), encoding="utf-8")
        result = {"success": True, "output": str(output), "format": fmt, "records": len(records)}
        if wrapper_path is not None:
            result["markdown_wrapper"] = str(wrapper_path)
        _emit(result, as_json=as_json)
        return

    if cmd == "contradictions":
        superseded = ledger.list_records(status="superseded")
        active_conflicts = ledger.find_active_conflicts()
        payload = {
            "superseded_count": len(superseded),
            "superseded_records": superseded,
            "active_conflict_count": active_conflicts["conflict_count"],
            "active_conflicts": active_conflicts["conflicts"],
        }
        _emit(payload, as_json=as_json)
        return

    raise SystemExit("Usage: hermes memory ledger {audit|search|add|contradictions}")

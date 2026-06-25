"""CLI helpers for `hermes memory snapshot ...`."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_cli.local_memory_ops.ledger import BeliefLedger
from hermes_cli.local_memory_ops.paths import default_memory_snapshot_dir


def _emit(payload: Dict[str, Any], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _metadata_wrapper_path(snapshot_path: Path) -> Path:
    suffix = snapshot_path.suffix.lstrip(".") or "snapshot"
    return snapshot_path.with_name(f"{snapshot_path.stem}-{suffix}.md")


def _default_snapshot_dir() -> Path:
    return default_memory_snapshot_dir()


def _iso_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _freshness_status(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "missing"
    if age_seconds <= 7 * 24 * 60 * 60:
        return "fresh"
    if age_seconds <= 30 * 24 * 60 * 60:
        return "stale"
    return "old"


def build_snapshot_status_report(
    *,
    snapshot_dir: Optional[Path | str] = None,
    current_time: Optional[float] = None,
) -> Dict[str, Any]:
    """Report freshness and sync posture for portable Memvid snapshots."""
    root = Path(snapshot_dir).expanduser() if snapshot_dir is not None else _default_snapshot_dir()
    now = float(current_time if current_time is not None else time.time())
    snapshots = []
    if root.exists():
        snapshots = sorted(root.glob("*.mv2"), key=lambda p: p.stat().st_mtime, reverse=True)
    latest = snapshots[0] if snapshots else None
    recommendations: list[Dict[str, str]] = []

    latest_payload = None
    wrapper_payload: Dict[str, Any] = {"present": False, "path": None, "expected_path": None}
    age_seconds = None
    if latest is None:
        recommendations.append({"code": "memvid_snapshot_missing", "severity": "warn"})
    else:
        stat = latest.stat()
        age_seconds = max(0.0, now - stat.st_mtime)
        expected_wrapper = _metadata_wrapper_path(latest)
        latest_payload = {
            "path": str(latest),
            "name": latest.name,
            "size_bytes": stat.st_size,
            "modified_at": _iso_from_epoch(stat.st_mtime),
            "age_seconds": int(age_seconds),
        }
        wrapper_payload = {
            "present": expected_wrapper.exists(),
            "path": str(expected_wrapper) if expected_wrapper.exists() else None,
            "expected_path": str(expected_wrapper),
        }
        if expected_wrapper.exists():
            wstat = expected_wrapper.stat()
            wrapper_payload.update({
                "size_bytes": wstat.st_size,
                "modified_at": _iso_from_epoch(wstat.st_mtime),
                "age_seconds": int(max(0.0, now - wstat.st_mtime)),
            })
        else:
            recommendations.append({"code": "memvid_wrapper_missing", "severity": "warn"})
        recommendations.append({
            "code": "raw_mv2_requires_nas_or_rsync_backup",
            "severity": "info",
            "message": "Obsidian Sync covers the Markdown wrapper; back up the raw .mv2 via NAS/rsync.",
        })

    status = _freshness_status(age_seconds)
    if status in {"stale", "old"}:
        recommendations.append({"code": f"memvid_snapshot_{status}", "severity": "warn"})

    return {
        "success": True,
        "snapshot_dir": str(root),
        "latest_snapshot": latest_payload,
        "wrapper": wrapper_payload,
        "freshness": {
            "status": status,
            "age_seconds": int(age_seconds) if age_seconds is not None else None,
            "fresh_threshold_seconds": 7 * 24 * 60 * 60,
            "stale_threshold_seconds": 30 * 24 * 60 * 60,
        },
        "counts": {
            "snapshots": len(snapshots),
            "wrappers": len(list(root.glob("*-mv2.md"))) if root.exists() else 0,
        },
        "recommendations": recommendations,
    }


def _record_to_text(row: Dict[str, Any]) -> str:
    return "\n".join(
        [
            f"Record ID: {row.get('id')}",
            f"Type: {row.get('type')}",
            f"Status: {row.get('status')}",
            f"Subject: {row.get('subject')}",
            f"Predicate: {row.get('predicate')}",
            f"Confidence: {row.get('confidence')}",
            f"Evidence: {row.get('evidence_ref')}",
            f"Source: {row.get('source')}",
            "",
            str(row.get("object") or ""),
        ]
    )


def _load_memvid_sdk(memvid_sdk: Any = None) -> Any:
    if memvid_sdk is not None:
        return memvid_sdk
    try:
        import memvid_sdk as imported_sdk  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised by integration environment differences
        raise SystemExit(
            "memvid-sdk is not installed. Install with: "
            "uv pip install --python /path/to/venv/bin/python memvid-sdk==2.0.159"
        ) from exc
    return imported_sdk


def _hits_to_text(find_result: Any) -> str:
    if isinstance(find_result, dict):
        hits = find_result.get("hits", [])
    else:
        hits = getattr(find_result, "hits", find_result)
    if not hits:
        return ""
    try:
        limited_hits = list(hits)[:3]
    except TypeError:
        limited_hits = []
    lines = []
    for hit in limited_hits:
        if isinstance(hit, dict):
            text = hit.get("text") or hit.get("snippet") or hit.get("content") or json.dumps(hit, ensure_ascii=False)
        else:
            text = getattr(hit, "text", None) or getattr(hit, "snippet", None) or getattr(hit, "content", None) or str(hit)
        lines.append(str(text))
    return "\n\n".join(lines)


def memory_snapshot_command(
    args,
    *,
    ledger: Optional[BeliefLedger] = None,
    memvid_sdk: Any = None,
) -> None:
    """Dispatch memory snapshot subcommands."""
    cmd = getattr(args, "snapshot_command", None)
    as_json = bool(getattr(args, "json", False))
    if cmd == "status":
        snapshot_dir = getattr(args, "dir", None)
        _emit(
            build_snapshot_status_report(snapshot_dir=snapshot_dir),
            as_json=as_json,
        )
        return
    if cmd != "create":
        raise SystemExit("Usage: hermes memory snapshot {create|status}")

    ledger = ledger or BeliefLedger()
    sdk = _load_memvid_sdk(memvid_sdk)
    output_arg = getattr(args, "output", None)
    output = Path(output_arg).expanduser() if output_arg else _default_snapshot_dir() / "memory-ledger.mv2"
    output.parent.mkdir(parents=True, exist_ok=True)
    records = ledger.list_records()
    created_at = datetime.now(timezone.utc).isoformat()
    memory = sdk.create(
        str(output),
        kind="basic",
        enable_vec=bool(getattr(args, "enable_vec", False)),
        enable_lex=True,
    )
    try:
        for row in records:
            memory.put(
                title=f"Memory ledger record {row.get('id')}",
                text=_record_to_text(row),
                tags=["hermes", "memory-ledger", str(row.get("status") or "unknown")],
                metadata={
                    "source": "hermes-memory-ledger",
                    "ledger_db": str(ledger.db_path),
                    "record_id": row.get("id"),
                    "subject": row.get("subject"),
                    "predicate": row.get("predicate"),
                    "status": row.get("status"),
                    "evidence_ref": row.get("evidence_ref"),
                    "snapshot_created_at": created_at,
                },
            )
        memory.commit()
        verify_result = memory.verify(deep=False)
        stats_result = memory.stats()
        query = getattr(args, "query", None) or "self-hosted memory"
        recall_text = _hits_to_text(memory.find(query, k=3)) if records else ""
    finally:
        close = getattr(memory, "close", None)
        if callable(close):
            close()

    wrapper = _metadata_wrapper_path(output)
    wrapper.write_text(
        "\n".join(
            [
                "# Memvid Memory Snapshot",
                "",
                "Portable Memvid snapshot generated from the local Hermes memory ledger.",
                "",
                f"- Snapshot: `{output}`",
                f"- Ledger DB: `{ledger.db_path}`",
                f"- Records: `{len(records)}`",
                f"- Created at: `{created_at}`",
                f"- Verification: `{verify_result}`",
                f"- Stats: `{stats_result}`",
                f"- Smoke query: `{query}`",
                "",
                "## Smoke recall",
                "",
                recall_text or "No recall result.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _emit(
        {
            "success": True,
            "output": str(output),
            "metadata_wrapper": str(wrapper),
            "records": len(records),
            "verify": verify_result,
            "stats": stats_result,
            "smoke_query": query,
            "smoke_recall": recall_text,
        },
        as_json=as_json,
    )

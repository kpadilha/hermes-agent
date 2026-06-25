"""CLI helpers for `hermes memory graph sync`.

This syncs the profile-scoped SQLite memory ledger into the local Neo4j graph
projection used by the isolated Graphiti pilot. The SQLite ledger remains the
canonical store; Neo4j is a projection with explicit provenance and status.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

from hermes_cli.local_memory_ops.ledger import BeliefLedger

GRAPH_ENV_FILE = Path(os.environ.get("GRAPHITI_NEO4J_ENV", "~/.config/hermes/graphiti-neo4j.env")).expanduser()
GRAPHITI_VENV = Path(os.environ.get("GRAPHITI_VENV", "/home/krishna/.hermes/graphiti-venv")).expanduser()


DriverFactory = Callable[[], Any]


def _record_projection(row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a ledger row into the Neo4j relationship projection shape."""
    return {
        "record_id": int(row.get("id")),
        "subject": str(row.get("subject") or "unknown"),
        "predicate": str(row.get("predicate") or "relates_to"),
        "object": str(row.get("object") or "unknown")[:240] or "unknown",
        "full_object": str(row.get("object") or ""),
        "type": row.get("type") or "fact",
        "status": row.get("status") or "active",
        "confidence": float(row.get("confidence") or 0.0),
        "source": row.get("source") or "",
        "evidence_ref": row.get("evidence_ref") or "",
        "supersedes": row.get("supersedes") or "",
        "contradicted_by": row.get("contradicted_by") or "",
        "valid_at": row.get("valid_from") or row.get("created_at"),
        "invalid_at": row.get("valid_until"),
        "created_at": row.get("created_at"),
        "last_seen_at": row.get("last_seen_at"),
        "storage_targets": row.get("storage_targets") or "",
    }


def _edge_fingerprint(edge: Dict[str, Any]) -> tuple[Any, ...]:
    return (
        int(edge.get("record_id")),
        edge.get("subject") or "",
        edge.get("predicate") or "",
        edge.get("object") or "",
        edge.get("status") or "",
        edge.get("evidence_ref") or "",
        edge.get("source") or "",
    )


def _active_krishna_preference_count(edges: Iterable[Dict[str, Any]]) -> int:
    return sum(
        1
        for edge in edges
        if edge.get("subject") == "Krishna"
        and edge.get("predicate") == "prefers"
        and edge.get("status") == "active"
    )


def _ledger_records(ledger: BeliefLedger) -> list[Dict[str, Any]]:
    return ledger.list_records()


def build_graph_sync_plan(
    *,
    ledger: Optional[BeliefLedger] = None,
    existing_edges: Optional[list[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return a dry-run delta plan from SQLite ledger rows to Neo4j edges."""
    ledger = ledger or BeliefLedger()
    records = sorted(_ledger_records(ledger), key=lambda r: int(r.get("id") or 0))
    desired = [_record_projection(row) for row in records]
    existing_edges = existing_edges or []
    existing_by_id = {int(edge.get("record_id")): edge for edge in existing_edges if edge.get("record_id") is not None}
    existing_fps = {_edge_fingerprint(edge) for edge in existing_edges if edge.get("record_id") is not None}

    changes: list[Dict[str, Any]] = []
    already_current = 0
    for edge in desired:
        current = existing_by_id.get(int(edge["record_id"]))
        if current and _edge_fingerprint(edge) in existing_fps:
            already_current += 1
            continue
        changes.append({
            "action": "upsert",
            "record_id": edge["record_id"],
            "status": edge["status"],
            "reason": "missing" if current is None else "changed",
            "edge": edge,
        })

    projected_edges = list(existing_edges)
    by_id = {int(edge.get("record_id")): dict(edge) for edge in projected_edges if edge.get("record_id") is not None}
    for change in changes:
        by_id[int(change["record_id"])] = change["edge"]
    final_projection = list(by_id.values()) if by_id else desired
    active_pref_count = _active_krishna_preference_count(final_projection)

    return {
        "success": True,
        "mode": "dry-run",
        "ledger_db": str(ledger.db_path),
        "summary": {
            "ledger_records": len(records),
            "existing_edges": len(existing_edges),
            "to_upsert": len(changes),
            "already_current": already_current,
        },
        "changes": changes,
        "validation": {
            "active_krishna_preference_edges": active_pref_count,
            "active_krishna_preference_ok": active_pref_count == 1,
        },
    }


def _load_env_file(path: Path = GRAPH_ENV_FILE) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env[key] = value
    return env


def _add_graphiti_venv_site_packages() -> None:
    if not GRAPHITI_VENV.exists():
        return
    candidates = sorted((GRAPHITI_VENV / "lib").glob("python*/site-packages"))
    for candidate in candidates:
        path = str(candidate)
        if path not in sys.path:
            sys.path.insert(0, path)


def _default_driver_factory() -> Any:
    env = _load_env_file()
    missing = [key for key in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD") if not env.get(key)]
    if missing:
        raise RuntimeError(f"Missing Graphiti Neo4j env values in {GRAPH_ENV_FILE}: {', '.join(missing)}")
    try:
        from neo4j import GraphDatabase  # type: ignore
    except Exception:
        _add_graphiti_venv_site_packages()
        from neo4j import GraphDatabase  # type: ignore
    return GraphDatabase.driver(env["NEO4J_URI"], auth=(env["NEO4J_USER"], env["NEO4J_PASSWORD"]))


def _fetch_existing_edges(session: Any) -> list[Dict[str, Any]]:
    rows = session.run(
        """
        MATCH (s:HermesEntity)-[r:HERMES_MEMORY_FACT]->(o:HermesEntity)
        RETURN r.record_id AS record_id, s.name AS subject, r.predicate AS predicate,
               o.name AS object, r.status AS status, r.evidence_ref AS evidence_ref,
               r.source AS source
        """
    ).data()
    return [dict(row) for row in rows]


def _ensure_graph_schema(session: Any) -> None:
    session.run("CREATE CONSTRAINT hermes_entity_name IF NOT EXISTS FOR (e:HermesEntity) REQUIRE e.name IS UNIQUE")
    session.run("CREATE INDEX hermes_memory_record_id IF NOT EXISTS FOR ()-[r:HERMES_MEMORY_FACT]-() ON (r.record_id)")


def _apply_change(session: Any, edge: Dict[str, Any], *, synced_at: str) -> None:
    session.run(
        """
        MERGE (s:HermesEntity {name: $subject})
        SET s.kind = 'subject'
        MERGE (o:HermesEntity {name: $object})
        SET o.kind = 'object', o.full_text = $full_object
        MERGE (s)-[r:HERMES_MEMORY_FACT {record_id: $record_id}]->(o)
        SET r.predicate = $predicate,
            r.type = $type,
            r.status = $status,
            r.confidence = $confidence,
            r.source = $source,
            r.evidence_ref = $evidence_ref,
            r.supersedes = $supersedes,
            r.contradicted_by = $contradicted_by,
            r.valid_at = $valid_at,
            r.invalid_at = $invalid_at,
            r.created_at = $created_at,
            r.last_seen_at = $last_seen_at,
            r.storage_targets = $storage_targets,
            r.graph_sync_loaded_at = $synced_at
        """,
        **edge,
        synced_at=synced_at,
    )


def _live_active_preference_count(session: Any) -> int:
    row = session.run(
        """
        MATCH (s:HermesEntity)-[r:HERMES_MEMORY_FACT]->(o:HermesEntity)
        WHERE s.name = 'Krishna' AND r.predicate = 'prefers' AND r.status = 'active'
        RETURN count(r) AS active_preference_edges
        """
    ).single()
    if not row:
        return 0
    try:
        return int(row["active_preference_edges"])
    except Exception:
        return int(row.get("active_preference_edges", 0))


def run_graph_sync(
    *,
    ledger: Optional[BeliefLedger] = None,
    apply: bool = False,
    driver_factory: Optional[DriverFactory] = None,
) -> Dict[str, Any]:
    """Plan or apply ledger -> Neo4j graph sync."""
    ledger = ledger or BeliefLedger()
    driver_factory = driver_factory or _default_driver_factory
    with driver_factory() as driver:
        driver.verify_connectivity()
        with driver.session(database="neo4j") as session:
            _ensure_graph_schema(session)
            existing_edges = _fetch_existing_edges(session)
            plan = build_graph_sync_plan(ledger=ledger, existing_edges=existing_edges)
            if not apply:
                return plan
            synced_at = datetime.now(timezone.utc).isoformat()
            upserted = 0
            for change in plan["changes"]:
                _apply_change(session, change["edge"], synced_at=synced_at)
                upserted += 1
            live_count = _live_active_preference_count(session)
            plan["mode"] = "apply"
            plan["applied"] = {"upserted": upserted, "synced_at": synced_at}
            plan["validation"] = {
                "active_krishna_preference_edges": live_count,
                "active_krishna_preference_ok": live_count == 1,
            }
            return plan


def _emit(payload: Dict[str, Any], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def memory_graph_command(
    args,
    *,
    ledger: Optional[BeliefLedger] = None,
    driver_factory: Optional[DriverFactory] = None,
) -> None:
    cmd = getattr(args, "graph_command", None)
    if cmd != "sync":
        raise SystemExit("Usage: hermes memory graph sync [--dry-run|--apply] [--json]")
    wants_apply = bool(getattr(args, "apply", False))
    wants_dry = bool(getattr(args, "dry_run", False))
    if wants_apply and wants_dry:
        raise SystemExit("Choose only one of --dry-run or --apply")
    payload = run_graph_sync(
        ledger=ledger,
        apply=wants_apply,
        driver_factory=driver_factory,
    )
    _emit(payload, as_json=bool(getattr(args, "json", False)))

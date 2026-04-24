"""Read-only reconciliation audit for Hermes memory layers."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from agent.memory_ledger import BeliefLedger
from tools.memory_tool import ENTRY_DELIMITER, get_memory_dir


def _read_entries(path: Path) -> list[str]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    return [part.strip() for part in raw.split(ENTRY_DELIMITER) if part.strip()]


def _contains_entry(entries: Iterable[str], haystack: Iterable[str]) -> list[str]:
    hay = "\n".join(haystack).lower()
    return [entry for entry in entries if entry.lower() not in hay]


def _path_age_seconds(path: Path) -> float | None:
    if not path.exists():
        return None
    try:
        return max(0.0, __import__("time").time() - path.stat().st_mtime)
    except OSError:
        return None


def _discover_snapshot_wrappers() -> list[Path]:
    root = Path("~/obsidian-vault/Krishna/niko/operations/memory-snapshots").expanduser()
    if not root.exists():
        return []
    return sorted(root.glob("*-mv2.md"), key=lambda p: p.stat().st_mtime, reverse=True)


def _discover_honcho_env() -> dict[str, str]:
    env_path = Path("/home/krishna/honcho/.env")
    result: dict[str, str] = {}
    if not env_path.exists():
        return result
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {
            "EMBEDDING_MODEL",
            "LLM_EMBEDDING_BASE_URL",
            "HONCHO_UNLOAD_EMBEDDING_MODEL_AFTER_REQUEST",
            "DERIVER_MODEL",
            "DERIVER_PROVIDER",
        }:
            result[key] = value
    return result


def _discover_ollama_models() -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["ollama", "ps"],
            check=False,
            text=True,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        return []
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    models = []
    for line in lines[1:]:
        name = line.split()[0] if line.split() else ""
        if name:
            models.append({"name": name, "raw": line})
    return models


def _discover_graph_facts() -> list[dict[str, Any]]:
    env_path = Path("~/.config/hermes/graphiti-neo4j.env").expanduser()
    python_path = Path("/home/krishna/.hermes/graphiti-venv/bin/python")
    if not env_path.exists() or not python_path.exists():
        return []
    code = r'''
import json
from pathlib import Path
from neo4j import GraphDatabase
env = {}
for line in Path("~/.config/hermes/graphiti-neo4j.env").expanduser().read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        env[k] = v
with GraphDatabase.driver(env["NEO4J_URI"], auth=(env["NEO4J_USER"], env["NEO4J_PASSWORD"])) as driver:
    driver.verify_connectivity()
    with driver.session(database="neo4j") as session:
        rows = session.run("""
        MATCH (s)-[r]->(o)
        WHERE type(r) IN ['HERMES_MEMORY_FACT', 'OLLAMA_EXTRACTED_FACT']
        RETURN coalesce(s.name, '') AS subject, coalesce(r.predicate, type(r)) AS predicate,
               coalesce(o.name, '') AS object, type(r) AS type, coalesce(r.status, '') AS status
        LIMIT 100
        """).data()
print(json.dumps(rows, ensure_ascii=False))
'''
    try:
        proc = subprocess.run(
            [str(python_path), "-c", code],
            check=False,
            text=True,
            capture_output=True,
            timeout=20,
        )
        if proc.returncode != 0:
            return []
        return json.loads(proc.stdout or "[]")
    except Exception:
        return []


def _honcho_http_json(method: str, url: str, payload: dict[str, Any] | None = None, timeout: int = 10) -> dict[str, Any]:
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def _normalize_honcho_card_payload(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        value = payload.get("peer_card") or payload.get("card") or payload.get("items") or []
    else:
        value = payload
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value:
        return [str(value)]
    return []


def _discover_honcho_peer_card(
    *,
    peer_id: str | None = None,
    workspace_id: str = "niko-main",
    base_url: str = "http://localhost:8000/v3",
    http_json=_honcho_http_json,
) -> list[str]:
    peer_id = peer_id or os.environ.get("HONCHO_USER_PEER_ID") or "96809052"
    endpoint = f"{base_url.rstrip('/')}/workspaces/{workspace_id}/peers/{peer_id}/card"
    try:
        return _normalize_honcho_card_payload(http_json("GET", endpoint, None, timeout=10))
    except Exception:
        return []


def _normalize_honcho_conclusion_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        value = payload.get("items") or payload.get("results") or payload.get("data") or []
    else:
        value = payload
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            normalized.append(dict(item))
        elif item:
            normalized.append({"content": str(item)})
    return normalized


def _conclusion_contents(items: Iterable[dict[str, Any]]) -> list[str]:
    return [str(item.get("content") or "") for item in items if str(item.get("content") or "").strip()]


def _discover_honcho_conclusions(
    *,
    observer_id: str = "niko",
    observed_id: str | None = None,
    workspace_id: str = "niko-main",
    base_url: str = "http://localhost:8000/v3",
    http_json=_honcho_http_json,
) -> list[str]:
    observed_id = observed_id or os.environ.get("HONCHO_USER_PEER_ID") or "96809052"
    endpoint = f"{base_url.rstrip('/')}/workspaces/{workspace_id}/conclusions/list?size=100"
    payload = {"filters": {"observer_id": observer_id, "observed_id": observed_id}}
    try:
        return _conclusion_contents(_normalize_honcho_conclusion_payload(http_json("POST", endpoint, payload, timeout=15)))
    except Exception:
        return []


def sync_honcho_conclusions_from_user_md(
    *,
    memory_dir: Optional[Path] = None,
    existing_conclusions: Optional[list[str]] = None,
    observer_id: str = "niko",
    observed_id: str = "96809052",
    workspace_id: str = "niko-main",
    base_url: str = "http://localhost:8000/v3",
    http_json=_honcho_http_json,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create missing USER.md entries as Honcho conclusions with visibility validation."""
    memory_dir = memory_dir or get_memory_dir()
    entries = _read_entries(memory_dir / "USER.md")
    existing = existing_conclusions if existing_conclusions is not None else _discover_honcho_conclusions(
        observer_id=observer_id,
        observed_id=observed_id,
        workspace_id=workspace_id,
        base_url=base_url,
        http_json=http_json,
    )
    missing = _contains_entry(entries, existing)
    endpoint = f"{base_url.rstrip('/')}/workspaces/{workspace_id}/conclusions"
    list_endpoint = f"{base_url.rstrip('/')}/workspaces/{workspace_id}/conclusions/list?size=100"
    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "observer_id": observer_id,
            "observed_id": observed_id,
            "workspace_id": workspace_id,
            "would_create_count": len(missing),
            "conclusions": missing,
            "endpoint": endpoint,
            "validation": {"all_visible": None, "missing_after_write": missing},
        }
    if not missing:
        return {
            "success": True,
            "dry_run": False,
            "observer_id": observer_id,
            "observed_id": observed_id,
            "workspace_id": workspace_id,
            "created_count": 0,
            "conclusions": [],
            "endpoint": endpoint,
            "validation": {"all_visible": True, "missing_after_write": []},
        }
    create_payload = {
        "conclusions": [
            {"content": item, "observer_id": observer_id, "observed_id": observed_id}
            for item in missing
        ]
    }
    created_payload = http_json("POST", endpoint, create_payload, timeout=30)
    created = _normalize_honcho_conclusion_payload(created_payload)
    list_payload = {"filters": {"observer_id": observer_id, "observed_id": observed_id}}
    visible_payload = http_json("POST", list_endpoint, list_payload, timeout=30)
    visible = _conclusion_contents(_normalize_honcho_conclusion_payload(visible_payload))
    still_missing = _contains_entry(missing, visible)
    all_visible = not still_missing
    return {
        "success": all_visible,
        "dry_run": False,
        "observer_id": observer_id,
        "observed_id": observed_id,
        "workspace_id": workspace_id,
        "created_count": len(created) if created else len(missing),
        "conclusions": missing,
        "endpoint": endpoint,
        "validation": {
            "all_visible": all_visible,
            "missing_after_write": still_missing,
            "visible_count": len(visible),
            "created_payload_count": len(created),
        },
    }


def sync_honcho_peer_card_from_user_md(
    *,
    memory_dir: Optional[Path] = None,
    peer_id: str = "96809052",
    workspace_id: str = "niko-main",
    base_url: str = "http://localhost:8000/v3",
    http_json=_honcho_http_json,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Sync canonical USER.md entries into one Honcho peer card with readback validation."""
    memory_dir = memory_dir or get_memory_dir()
    entries = _read_entries(memory_dir / "USER.md")
    endpoint = f"{base_url.rstrip('/')}/workspaces/{workspace_id}/peers/{peer_id}/card"
    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "peer_id": peer_id,
            "workspace_id": workspace_id,
            "would_write_count": len(entries),
            "endpoint": endpoint,
            "validation": {"card_matches_local": None, "readback_card": []},
        }

    http_json("PUT", endpoint, {"peer_card": entries}, timeout=15)
    readback_payload = http_json("GET", endpoint, None, timeout=15)
    readback = _normalize_honcho_card_payload(readback_payload)
    missing = _contains_entry(entries, readback)
    extra = _contains_entry(readback, entries)
    matches = not missing and not extra and len(readback) == len(entries)
    return {
        "success": matches,
        "dry_run": False,
        "peer_id": peer_id,
        "workspace_id": workspace_id,
        "written_count": len(entries),
        "endpoint": endpoint,
        "validation": {
            "card_matches_local": matches,
            "readback_card": readback,
            "missing_after_readback": missing,
            "extra_after_readback": extra,
            "expected_count": len(entries),
            "readback_count": len(readback),
        },
    }


def build_memory_reconcile_report(
    *,
    memory_dir: Optional[Path] = None,
    ledger: Optional[BeliefLedger] = None,
    graph_facts: Optional[list[dict[str, Any]]] = None,
    honcho_card: Optional[list[str]] = None,
    honcho_conclusions: Optional[list[str]] = None,
    ollama_models: Optional[list[dict[str, Any]]] = None,
    snapshot_wrappers: Optional[list[Path]] = None,
    honcho_env: Optional[dict[str, str]] = None,
) -> Dict[str, Any]:
    memory_dir = memory_dir or get_memory_dir()
    ledger = ledger or BeliefLedger()
    graph_facts = _discover_graph_facts() if graph_facts is None else graph_facts
    honcho_card = _discover_honcho_peer_card() if honcho_card is None else honcho_card
    honcho_conclusions = _discover_honcho_conclusions() if honcho_conclusions is None else honcho_conclusions
    ollama_models = _discover_ollama_models() if ollama_models is None else ollama_models
    snapshot_wrappers = _discover_snapshot_wrappers() if snapshot_wrappers is None else snapshot_wrappers
    honcho_env = _discover_honcho_env() if honcho_env is None else honcho_env

    user_entries = _read_entries(memory_dir / "USER.md")
    memory_entries = _read_entries(memory_dir / "MEMORY.md")
    ledger_audit = ledger.audit()
    active_conflicts = ledger.find_active_conflicts()
    ledger_records = ledger.search("", limit=10000)
    graph_texts = [json.dumps(f, ensure_ascii=False) for f in graph_facts]
    model_names = {m.get("name") for m in ollama_models}
    latest_wrapper = snapshot_wrappers[0] if snapshot_wrappers else None

    divergence = {
        "user_missing_in_honcho_card": _contains_entry(user_entries, honcho_card),
        "user_missing_in_honcho_conclusions": _contains_entry(user_entries, honcho_conclusions),
        "ledger_active_conflicts": active_conflicts,
        "ledger_missing_in_graphiti": _contains_entry(
            [str(r.get("object") or "") for r in ledger_records if r.get("status") == "active"],
            graph_texts,
        ),
    }
    recommendations = []
    if divergence["user_missing_in_honcho_card"]:
        recommendations.append({"code": "honcho_card_missing_user_entries", "severity": "warn"})
    if divergence["user_missing_in_honcho_conclusions"]:
        recommendations.append({"code": "honcho_conclusions_missing_user_entries", "severity": "info"})
    if active_conflicts.get("conflict_count", 0):
        recommendations.append({"code": "ledger_active_conflicts", "severity": "fail"})
    if divergence["ledger_missing_in_graphiti"]:
        recommendations.append({"code": "graphiti_projection_stale", "severity": "warn"})
    if not latest_wrapper:
        recommendations.append({"code": "memvid_snapshot_missing", "severity": "warn"})
    if honcho_env.get("HONCHO_UNLOAD_EMBEDDING_MODEL_AFTER_REQUEST", "").lower() != "true":
        recommendations.append({"code": "honcho_embedding_unload_disabled", "severity": "warn"})
    if "nomic-embed-text:latest" in model_names:
        recommendations.append({"code": "ollama_embedding_model_resident", "severity": "warn"})
    if "phi4-mini:latest" not in model_names:
        recommendations.append({"code": "ollama_phi4_cron_model_not_resident", "severity": "info"})
    if "qwen3.5:latest" not in model_names:
        recommendations.append({"code": "ollama_qwen35_graphiti_model_not_resident", "severity": "info"})

    return {
        "success": True,
        "sources": {
            "user_md": {"path": str(memory_dir / "USER.md"), "count": len(user_entries)},
            "memory_md": {"path": str(memory_dir / "MEMORY.md"), "count": len(memory_entries)},
            "ledger": {"db_path": str(ledger.db_path), "records": sum(ledger_audit.get("records", {}).values()) if isinstance(ledger_audit.get("records"), dict) else 0, "audit": ledger_audit},
            "honcho": {"peer_card_count": len(honcho_card), "conclusion_count": len(honcho_conclusions)},
            "graphiti": {"facts": len(graph_facts)},
            "memvid": {"latest_wrapper": str(latest_wrapper) if latest_wrapper else None},
        },
        "freshness": {
            "memvid_latest_wrapper_age_seconds": _path_age_seconds(latest_wrapper) if latest_wrapper else None,
        },
        "runtime": {
            "ollama": {
                "models": sorted(model_names),
                "phi4_loaded": "phi4-mini:latest" in model_names,
                "qwen35_loaded": "qwen3.5:latest" in model_names,
                "nomic_loaded": "nomic-embed-text:latest" in model_names,
            },
            "honcho": {
                "embedding_model": honcho_env.get("EMBEDDING_MODEL"),
                "embedding_base_url": honcho_env.get("LLM_EMBEDDING_BASE_URL"),
                "unload_embedding_after_request": honcho_env.get("HONCHO_UNLOAD_EMBEDDING_MODEL_AFTER_REQUEST", "").lower() == "true",
                "deriver_model": honcho_env.get("DERIVER_MODEL"),
                "deriver_provider": honcho_env.get("DERIVER_PROVIDER"),
            },
        },
        "divergence": divergence,
        "recommendations": recommendations,
    }


def build_lcm_memory_state(report: Dict[str, Any]) -> Dict[str, Any]:
    """Build a fresh LCM memory proof surface from the live reconcile report."""
    divergence = report.get("divergence") or {}
    recommendations = report.get("recommendations") or []
    fail_recs = [r for r in recommendations if isinstance(r, dict) and r.get("severity") == "fail"]
    missing_card = divergence.get("user_missing_in_honcho_card") or []
    missing_conclusions = divergence.get("user_missing_in_honcho_conclusions") or []
    missing_graph = divergence.get("ledger_missing_in_graphiti") or []
    success = not fail_recs and not missing_card and not missing_conclusions and not missing_graph
    event = {
        "workflow": "memory_reconcile_projection",
        "outcome": "success" if success else "failure",
        "failure_class": None if success else "projection_divergence",
        "details": {
            "missing_in_honcho_card": missing_card,
            "missing_as_conclusions": missing_conclusions,
            "ledger_missing_in_graphiti": missing_graph,
            "recommendation_codes": [str(r.get("code")) for r in recommendations if isinstance(r, dict)],
        },
    }
    counters = {"memory_reconcile_projection": {"success": 1 if success else 0, "failure": 0 if success else 1}}
    return {
        "workflow_counters": counters,
        "recent_workflow_events": [event],
        "scorecard": {
            "overall": {
                "success": counters["memory_reconcile_projection"]["success"],
                "failure": counters["memory_reconcile_projection"]["failure"],
                "total": 1,
                "success_rate_pct": 100.0 if success else 0.0,
            },
            "workflows": {
                "memory_reconcile_projection": {
                    "success": counters["memory_reconcile_projection"]["success"],
                    "failure": counters["memory_reconcile_projection"]["failure"],
                    "total": 1,
                    "success_rate_pct": 100.0 if success else 0.0,
                }
            },
            "memory_sync_health": "ok" if success else "degraded",
        },
    }


def build_fix_plan(report: Dict[str, Any], *, dry_run: bool = True) -> Dict[str, Any]:
    """Build a conservative, non-mutating remediation plan for reconcile findings."""
    recommendations = report.get("recommendations") or []
    codes = {str(item.get("code") or "") for item in recommendations if isinstance(item, dict)}
    divergence = report.get("divergence") or {}
    runtime = report.get("runtime") or {}
    ollama = runtime.get("ollama") or {}
    actions: list[dict[str, Any]] = []

    if "honcho_card_missing_user_entries" in codes:
        actions.append({
            "id": "sync_honcho_peer_card_from_user_md",
            "description": "Synchronize the Honcho user peer card from canonical USER.md entries.",
            "reason": "USER.md is authoritative; Honcho card is its semantic projection.",
            "items": divergence.get("user_missing_in_honcho_card") or [],
            "command": "hermes memory sync-user-profile --dry-run  # proposed future/apply surface",
            "mutates": False,
        })
    if "honcho_conclusions_missing_user_entries" in codes:
        actions.append({
            "id": "add_missing_honcho_conclusions",
            "description": "Add missing durable USER.md facts as Honcho conclusions after peer-card sync validates.",
            "reason": "Conclusions provide longitudinal semantic recall, but should not proceed if card projection fails.",
            "items": divergence.get("user_missing_in_honcho_conclusions") or [],
            "command": "honcho_conclude(...) for each approved missing fact  # proposed only",
            "mutates": False,
        })
    if "graphiti_projection_stale" in codes:
        actions.append({
            "id": "sync_memory_ledger_to_graphiti",
            "description": "Project active ledger records into Neo4j/Graphiti.",
            "reason": "SQLite ledger is canonical; Graphiti is a projection and can be refreshed safely through dry-run/apply sync.",
            "items": divergence.get("ledger_missing_in_graphiti") or [],
            "command": "hermes memory graph sync --dry-run --json",
            "mutates": False,
        })
    if "ollama_embedding_model_resident" in codes or ollama.get("nomic_loaded"):
        actions.append({
            "id": "unload_transient_ollama_embedding_model",
            "description": "Unload nomic-embed-text if it remains resident after embedding requests.",
            "reason": "Embedding model should be transient to preserve VRAM for phi4-mini cron and qwen3.5 Graphiti extraction.",
            "items": ["nomic-embed-text:latest"],
            "command": "hermes ollama residency --fix --json",
            "mutates": False,
        })

    ignored = []
    for code in sorted(codes):
        if code in {"ollama_phi4_cron_model_not_resident", "ollama_qwen35_graphiti_model_not_resident"}:
            ignored.append({
                "code": code,
                "reason": "Model residency is idle-dependent; do not force-load large models in a reconcile fix plan.",
            })

    return {
        "dry_run": dry_run,
        "apply_supported": False,
        "proposed_actions": actions,
        "ignored_recommendations": ignored,
        "notes": [
            "This plan is intentionally non-mutating. Use dedicated commands after reviewing proposed actions.",
            "Direct --apply is not supported yet; this prevents silent cross-store mutation.",
        ],
    }


def memory_reconcile_command(
    args,
    *,
    memory_dir: Optional[Path] = None,
    ledger: Optional[BeliefLedger] = None,
    graph_facts: Optional[list[dict[str, Any]]] = None,
    honcho_card: Optional[list[str]] = None,
    honcho_conclusions: Optional[list[str]] = None,
    ollama_models: Optional[list[dict[str, Any]]] = None,
    snapshot_wrappers: Optional[list[Path]] = None,
    honcho_env: Optional[dict[str, str]] = None,
    peer_card_syncer=sync_honcho_peer_card_from_user_md,
    conclusion_syncer=sync_honcho_conclusions_from_user_md,
    runtime_status_writer=None,
) -> None:
    payload = build_memory_reconcile_report(
        memory_dir=memory_dir,
        ledger=ledger,
        graph_facts=graph_facts,
        honcho_card=honcho_card,
        honcho_conclusions=honcho_conclusions,
        ollama_models=ollama_models,
        snapshot_wrappers=snapshot_wrappers,
        honcho_env=honcho_env,
    )
    if bool(getattr(args, "fix", False)) and not bool(getattr(args, "dry_run", False)):
        print("error: --fix currently requires --dry-run; direct apply is intentionally unsupported", file=__import__("sys").stderr)
        raise SystemExit(2)
    apply_action = str(getattr(args, "apply_action", "") or "")
    if apply_action:
        if apply_action == "sync_honcho_peer_card_from_user_md":
            payload["apply_result"] = peer_card_syncer(
                memory_dir=memory_dir,
                peer_id=str(getattr(args, "honcho_peer", "") or "96809052"),
                dry_run=bool(getattr(args, "dry_run", False)),
            )
        elif apply_action == "add_missing_honcho_conclusions":
            payload["apply_result"] = conclusion_syncer(
                memory_dir=memory_dir,
                observed_id=str(getattr(args, "honcho_peer", "") or "96809052"),
                dry_run=bool(getattr(args, "dry_run", False)),
            )
        else:
            print(f"error: unsupported apply action: {apply_action}", file=__import__("sys").stderr)
            raise SystemExit(2)
    if bool(getattr(args, "fix", False)):
        payload["fix_plan"] = build_fix_plan(payload, dry_run=True)
    lcm_memory = build_lcm_memory_state(payload)
    payload["lcm_memory"] = lcm_memory
    if runtime_status_writer is None:
        try:
            from gateway.status import write_runtime_status
            runtime_status_writer = write_runtime_status
        except Exception:
            runtime_status_writer = None
    if runtime_status_writer is not None:
        try:
            runtime_status_writer(lcm_memory=lcm_memory)
        except Exception:
            pass
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print(json.dumps(payload, indent=2, ensure_ascii=False))

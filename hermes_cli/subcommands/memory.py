"""``hermes memory`` subcommand parser.

Extracted from ``hermes_cli/main.py:main()`` (god-file Phase 2 follow-up).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_memory_parser(subparsers, *, cmd_memory: Callable) -> None:
    """Attach the ``memory`` subcommand to ``subparsers``."""
    memory_parser = subparsers.add_parser(
        "memory",
        help="Configure external memory provider",
        description=(
            "Set up and manage external memory provider plugins.\n\n"
            "Available providers: honcho, openviking, mem0, hindsight,\n"
            "holographic, retaindb, byterover.\n\n"
            "Only one external provider can be active at a time.\n"
            "Built-in memory (MEMORY.md/USER.md) is always active."
        ),
    )
    memory_sub = memory_parser.add_subparsers(dest="memory_command")
    _setup_parser = memory_sub.add_parser(
        "setup", help="Interactive provider selection and configuration"
    )
    _setup_parser.add_argument(
        "provider",
        nargs="?",
        default=None,
        help="Provider to configure directly (e.g. honcho), skipping the picker",
    )
    memory_sub.add_parser("status", help="Show current memory provider config")
    memory_sub.add_parser("off", help="Disable external provider (built-in only)")
    eval_parser = memory_sub.add_parser(
        "eval",
        help="Run deterministic local Krishna MemoryEval checks",
    )
    eval_parser.add_argument("--json", action="store_true", help="Output JSON")
    reconcile_parser = memory_sub.add_parser(
        "reconcile",
        help="Read-only reconciliation audit across local memory projections",
    )
    reconcile_parser.add_argument("--json", action="store_true", help="Output JSON")
    reconcile_parser.add_argument("--fix", action="store_true", help="Build a conservative remediation plan")
    reconcile_parser.add_argument("--dry-run", action="store_true", help="Do not mutate stores; required with --fix")
    reconcile_parser.add_argument("--apply-action", default="", help="Apply one reviewed reconcile action")
    reconcile_parser.add_argument("--honcho-peer", default="96809052", help="Honcho peer id for peer-card apply action")
    graph_parser = memory_sub.add_parser(
        "graph",
        help="Sync the local memory ledger into the Neo4j/Graphiti projection",
    )
    graph_sub = graph_parser.add_subparsers(dest="graph_command")
    graph_sync = graph_sub.add_parser("sync", help="Delta sync SQLite memory ledger to Neo4j")
    graph_sync_mode = graph_sync.add_mutually_exclusive_group()
    graph_sync_mode.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    graph_sync_mode.add_argument("--apply", action="store_true", help="Apply changes to Neo4j")
    graph_sync.add_argument("--json", action="store_true", help="Output JSON")
    ledger_parser = memory_sub.add_parser(
        "ledger",
        help="Inspect the local structured belief/evidence ledger",
    )
    ledger_sub = ledger_parser.add_subparsers(dest="ledger_command")
    ledger_audit = ledger_sub.add_parser("audit", help="Show ledger counts and recent decisions")
    ledger_audit.add_argument("--json", action="store_true", help="Output JSON")
    ledger_search = ledger_sub.add_parser("search", help="Search memory records")
    ledger_search.add_argument("query", help="Search query")
    ledger_search.add_argument("--limit", type=int, default=20, help="Maximum records")
    ledger_search.add_argument("--json", action="store_true", help="Output JSON")
    ledger_add = ledger_sub.add_parser("add", help="Add a record through the write gate")
    ledger_add.add_argument("content", help="Memory content")
    ledger_add.add_argument("--target", choices=["memory", "user"], default="memory")
    ledger_add.add_argument("--source", default="cli:memory-ledger:add")
    ledger_add.add_argument("--evidence-ref", default="cli:memory-ledger:add")
    ledger_add.add_argument("--json", action="store_true", help="Output JSON")
    ledger_update = ledger_sub.add_parser("update", help="Update an active ledger record")
    ledger_update.add_argument("record_id", type=int, help="Record ID")
    ledger_update.add_argument("content", help="Updated content")
    ledger_update.add_argument("--source", default="cli:memory-ledger:update")
    ledger_update.add_argument("--evidence-ref", default="cli:memory-ledger:update")
    ledger_update.add_argument("--json", action="store_true", help="Output JSON")
    ledger_delete = ledger_sub.add_parser("delete", help="Mark a ledger record deleted")
    ledger_delete.add_argument("record_id", type=int, help="Record ID")
    ledger_delete.add_argument("--source", default="cli:memory-ledger:delete")
    ledger_delete.add_argument("--evidence-ref", default="cli:memory-ledger:delete")
    ledger_delete.add_argument("--json", action="store_true", help="Output JSON")
    ledger_promote = ledger_sub.add_parser("promote", help="Render a KB promotion candidate for a record")
    ledger_promote.add_argument("record_id", type=int, help="Record ID")
    ledger_promote.add_argument("--json", action="store_true", help="Output JSON")
    ledger_export = ledger_sub.add_parser("export", help="Export a local ledger projection")
    ledger_export.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ledger_export.add_argument("--output", required=True, help="Output file path")
    ledger_export.add_argument("--markdown-wrapper", action="store_true", help="Also write an Obsidian-syncable Markdown wrapper when exporting JSON")
    ledger_export.add_argument("--json", action="store_true", help="Output command result as JSON")
    ledger_contra = ledger_sub.add_parser("contradictions", help="Show superseded/contradicted records")
    ledger_contra.add_argument("--json", action="store_true", help="Output JSON")
    snapshot_parser = memory_sub.add_parser(
        "snapshot",
        help="Create portable memory snapshots from the local structured ledger",
    )
    snapshot_sub = snapshot_parser.add_subparsers(dest="snapshot_command")
    snapshot_create = snapshot_sub.add_parser("create", help="Create a Memvid snapshot from the memory ledger")
    snapshot_create.add_argument("--output", default="~/obsidian-vault/Krishna/niko/operations/memory-snapshots/memory-ledger.mv2", help="Output .mv2 path")
    snapshot_create.add_argument("--query", default="self-hosted memory", help="Smoke recall query")
    snapshot_create.add_argument("--enable-vec", action="store_true", help="Enable Memvid vector index if local embedding support is configured")
    snapshot_create.add_argument("--json", action="store_true", help="Output JSON")
    snapshot_status = snapshot_sub.add_parser("status", help="Report latest Memvid snapshot freshness")
    snapshot_status.add_argument("--dir", default="~/obsidian-vault/Krishna/niko/operations/memory-snapshots", help="Snapshot directory")
    snapshot_status.add_argument("--json", action="store_true", help="Output JSON")
    _reset_parser = memory_sub.add_parser(
        "reset",
        help="Erase all built-in memory (MEMORY.md and USER.md)",
    )
    _reset_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompt",
    )
    _reset_parser.add_argument(
        "--target",
        choices=["all", "memory", "user"],
        default="all",
        help="Which store to reset: 'all' (default), 'memory', or 'user'",
    )
    memory_parser.set_defaults(func=cmd_memory)

"""``hermes memory`` subcommand parser."""

from __future__ import annotations

from typing import Callable

from hermes_cli.subcommands._shared import add_yes_flag


def build_memory_parser(subparsers, *, cmd_memory: Callable) -> None:
    """Attach the ``memory`` subcommand to ``subparsers``."""
    memory_parser = subparsers.add_parser(
        "memory", help="Configure external memory provider",
        description="Set up and manage external memory provider plugins.\n\n"
            "Bundled providers: retaindb, byterover.\n"
            "Catalog providers (e.g. honcho, hindsight, supermemory,\n"
            "mem0, openviking, holographic): hermes plugins install <name>.\n\n"
            "Only one external provider can be active at a time.\n"
            "Built-in memory (MEMORY.md/USER.md) is always active.")
    memory_sub = memory_parser.add_subparsers(dest="memory_command")
    _setup_parser = memory_sub.add_parser(
        "setup", help="Interactive provider selection and configuration")
    _setup_parser.add_argument(
        "provider", nargs="?", default=None,
        help="Provider to configure directly (e.g. honcho), skipping the picker")
    memory_sub.add_parser("status", help="Show current memory provider config")
    memory_sub.add_parser("off", help="Disable external provider (built-in only)")
    _reset_parser = memory_sub.add_parser(
        "reset", help="Erase all built-in memory (MEMORY.md and USER.md)")
    add_yes_flag(_reset_parser)
    _reset_parser.add_argument(
        "--target", choices=["all", "memory", "user"], default="all",
        help="Which store to reset: 'all' (default), 'memory', or 'user'")
    memory_parser.set_defaults(func=cmd_memory)
    # Native CLI registrars only own top-level commands, and discovery normally
    # skips built-ins. Reuse the registered alias on this existing parser to
    # preserve local `memory ...` callers without importing plugin code here.
    from hermes_cli.plugins import discover_plugins, get_plugin_manager

    discover_plugins()
    extension = get_plugin_manager()._cli_commands.get("memory-local")
    if extension is not None:
        extension["setup_fn"](memory_parser)

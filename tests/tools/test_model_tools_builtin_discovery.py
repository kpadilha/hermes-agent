"""Regression coverage for lazy built-in registration during tool snapshots."""

import sys

import model_tools
from tools.registry import registry


def test_get_tool_definitions_rediscovers_missing_builtin_memory():
    """A late agent snapshot must recover a built-in missed during startup."""
    with registry._lock:
        original = registry._tools.pop("memory")
        registry._generation += 1
    sys.modules.pop("tools.memory_tool", None)
    model_tools._clear_tool_defs_cache()

    try:
        names = {
            item["function"]["name"]
            for item in model_tools.get_tool_definitions(
                enabled_toolsets=["memory", "file"],
                quiet_mode=True,
            )
        }
        assert "memory" in names
    finally:
        with registry._lock:
            if "memory" not in registry._tools:
                registry._tools["memory"] = original
                registry._generation += 1
        model_tools._clear_tool_defs_cache()

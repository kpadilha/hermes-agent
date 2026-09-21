"""Tests for delegate_tool toolset scoping.

Verifies that subagents cannot gain tools that the parent does not have.
The LLM controls the `toolsets` parameter — without intersection with the
parent's enabled_toolsets, it can escalate privileges by requesting
arbitrary toolsets.
"""

from types import SimpleNamespace

from tools.delegate_tool import _strip_blocked_tools, _emit_parent_console
from tools.delegate_tool_toolsets import _resolve_child_toolsets


class TestToolsetIntersection:
    """Subagent toolsets must be a subset of parent's enabled_toolsets."""



    def test_strip_blocked_removes_delegation(self):
        """Blocked toolsets (delegation, clarify, etc.) are always removed."""
        child = _strip_blocked_tools(["terminal", "delegation", "clarify", "memory"])
        assert "delegation" not in child
        assert "clarify" not in child
        assert "memory" not in child
        assert "terminal" in child



    def test_inherited_toolsets_drop_mcp_when_opted_out(self, monkeypatch):
        """The opt-out applies when the child inherits the parent's whole surface."""
        parent = SimpleNamespace(
            enabled_toolsets=["terminal", "web", "mcp-beehiiv"],
            disabled_toolsets=[],
        )
        monkeypatch.setattr(
            "tools.delegate_tool_toolsets._get_inherit_mcp_toolsets", lambda: False
        )

        enabled, _ = _resolve_child_toolsets(parent, None, "leaf")

        assert "mcp-beehiiv" not in enabled
        assert "terminal" in enabled
        assert "web" in enabled

    def test_all_tools_parent_drops_derived_mcp_when_opted_out(self, monkeypatch):
        """enabled_toolsets=None derives toolsets from loaded names; MCP still must be removed."""
        parent = SimpleNamespace(
            enabled_toolsets=None,
            disabled_toolsets=[],
            valid_tool_names={"terminal", "web_search", "mcp__beehiiv__list_posts"},
        )
        mapping = {
            "terminal": "terminal",
            "web_search": "web",
            "mcp__beehiiv__list_posts": "mcp-beehiiv",
        }
        monkeypatch.setattr("model_tools.get_toolset_for_tool", mapping.get)
        monkeypatch.setattr(
            "tools.delegate_tool_toolsets._get_inherit_mcp_toolsets", lambda: False
        )

        enabled, _ = _resolve_child_toolsets(parent, None, "leaf")

        assert "mcp-beehiiv" not in enabled
        assert "terminal" in enabled
        assert "web" in enabled


class TestEmitParentConsole:
    """Progress lines (e.g. ``✓ [N/M] …``) must route through the parent's
    configured ``_safe_print`` in headless stdio hosts (ACP, gateway) so
    they don't land on stdout and corrupt JSON-RPC frames. Regression for a
    bug where delegate_task completion lines pushed to stdout caused
    ``Failed to parse JSON message: ✓ [3/3] …`` errors in the ACP adapter."""

    def test_routes_through_parent_safe_print_when_available(self, capsys):
        captured_lines = []
        parent = SimpleNamespace(_safe_print=lambda line: captured_lines.append(line))

        _emit_parent_console(parent, "  ✓ [1/3] Research done  (11.55s)")

        assert captured_lines == ["  ✓ [1/3] Research done  (11.55s)"]
        stdout_stderr = capsys.readouterr()
        assert stdout_stderr.out == ""
        assert stdout_stderr.err == ""



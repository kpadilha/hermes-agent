from types import SimpleNamespace

import pytest

from hermes_cli import main_agent_cmds


@pytest.mark.parametrize(
    ("subcommand", "module_name", "handler_name"),
    [
        ("eval", "hermes_cli.local_memory_ops.eval_cmd", "memory_eval_command"),
        ("reconcile", "hermes_cli.local_memory_ops.reconcile_cmd", "memory_reconcile_command"),
        ("graph", "hermes_cli.local_memory_ops.graph_cmd", "memory_graph_command"),
        ("ledger", "hermes_cli.local_memory_ops.ledger_cmd", "memory_ledger_command"),
        ("snapshot", "hermes_cli.local_memory_ops.snapshot_cmd", "memory_snapshot_command"),
    ],
)
def test_cmd_memory_dispatches_registered_local_subcommands(
    monkeypatch, subcommand, module_name, handler_name
):
    calls = []

    def handler(args):
        calls.append(args)

    def fake_import(name):
        assert name == module_name
        return SimpleNamespace(**{handler_name: handler})

    monkeypatch.setattr(main_agent_cmds, "import_module", fake_import, raising=False)
    args = SimpleNamespace(memory_command=subcommand)
    main_agent_cmds.cmd_memory(args)
    assert calls == [args]

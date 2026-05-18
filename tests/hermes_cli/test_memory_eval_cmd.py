"""Tests for `hermes memory eval`."""

import json
from types import SimpleNamespace

from hermes_cli.memory_eval_cmd import memory_eval_command


def test_memory_eval_command_outputs_json(tmp_path, capsys):
    hermes_home = tmp_path / ".hermes"
    mem = hermes_home / "memories"
    mem.mkdir(parents=True)
    (mem / "USER.md").write_text("Krishna prefers self-hosted local-first memory. Telegram idle reset.\n", encoding="utf-8")
    (mem / "MEMORY.md").write_text("USER.md source; Honcho projection. LCM active.\n", encoding="utf-8")
    kb = tmp_path / "kb"
    note = kb / "wiki" / "operations" / "hermes-memory-architecture.md"
    note.parent.mkdir(parents=True)
    note.write_text("KB Compiled Truth. Memory Write Gate and Belief Ledger.\n", encoding="utf-8")

    memory_eval_command(SimpleNamespace(json=True), hermes_home=hermes_home, kb_root=kb)

    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["score_pct"] == 100.0

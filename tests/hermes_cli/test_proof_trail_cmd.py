import json
from pathlib import Path
from types import SimpleNamespace

from hermes_cli.proof_trail_cmd import (
    build_proof_markdown,
    create_proof_record,
    proof_trail_command,
    slugify_title,
)


def test_slugify_title_is_filesystem_safe_and_stable():
    assert slugify_title("Memory Autonomy: Task/LCM Proof Trail!") == "memory-autonomy-task-lcm-proof-trail"
    assert slugify_title("  ") == "proof"


def test_build_proof_markdown_contains_required_sections():
    markdown = build_proof_markdown(
        title="Memory Autonomy Proof Trail",
        status="validated",
        rationale="Need durable proof for autonomous actions.",
        inputs=["handoff.md", "plan.md"],
        files=["hermes_cli/proof_trail_cmd.py"],
        commands=["pytest tests/hermes_cli/test_proof_trail_cmd.py -q"],
        validations=["4 passed"],
        kb_promotions=["[[hermes-memory-architecture]]"],
        final_state="Ready for future tasks.",
        lcm_refs=["node:3"],
        timestamp="2026-04-24T20:00:00+00:00",
    )

    for heading in [
        "## Rationale",
        "## Inputs",
        "## Files Changed",
        "## Commands / Evidence",
        "## Validation",
        "## KB Promotion",
        "## LCM / Context References",
        "## Final State",
    ]:
        assert heading in markdown
    assert "status: \"validated\"" in markdown
    assert "- `hermes_cli/proof_trail_cmd.py`" in markdown
    assert "```bash\npytest tests/hermes_cli/test_proof_trail_cmd.py -q\n```" in markdown


def test_create_proof_record_writes_markdown_and_json_index(tmp_path):
    result = create_proof_record(
        title="Autonomy Monitor Implementation",
        status="validated",
        rationale="Record proof trail.",
        inputs=["handoff"],
        files=["script.py"],
        commands=["pytest -q"],
        validations=["passed"],
        kb_promotions=["health-check"],
        final_state="done",
        lcm_refs=["summary-node-3"],
        output_dir=tmp_path,
        timestamp="2026-04-24T20:00:00+00:00",
    )

    proof_path = Path(result["path"])
    index_path = tmp_path / "proof-index.json"
    assert proof_path.exists()
    assert proof_path.name == "2026-04-24-autonomy-monitor-implementation.md"
    assert index_path.exists()
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["proofs"][0]["title"] == "Autonomy Monitor Implementation"
    assert index["proofs"][0]["status"] == "validated"


def test_proof_trail_command_outputs_json(tmp_path, capsys):
    args = SimpleNamespace(
        title="Proof CLI Smoke",
        status="validated",
        rationale="Smoke proof command.",
        inputs=["input-a"],
        files=["file-a"],
        commands=["cmd-a"],
        validations=["ok"],
        kb_promotions=["kb-a"],
        final_state="complete",
        lcm_refs=["lcm-a"],
        output_dir=str(tmp_path),
        json=True,
    )

    proof_trail_command(args, timestamp="2026-04-24T20:00:00+00:00")

    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert Path(payload["path"]).exists()

import ast
from pathlib import Path

from hermes_cli.config import DEFAULT_CONFIG


def test_default_config_has_one_canonical_source():
    config_path = Path(__file__).parents[2] / "hermes_cli" / "config.py"
    tree = ast.parse(config_path.read_text(encoding="utf-8"))
    duplicate_assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "DEFAULT_CONFIG"
            for target in node.targets
        )
    ]

    assert duplicate_assignments == []
    assert DEFAULT_CONFIG["compression"]["relevance_pinning"]["enabled"] is False
    assert DEFAULT_CONFIG["security"]["gliguard"]["enabled"] is False

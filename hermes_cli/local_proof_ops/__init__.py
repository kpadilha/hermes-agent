"""Local Niko proof-trail operations.

This package holds Krishna/Niko's local proof-trail implementation while the
public CLI compatibility surface remains ``hermes_cli.proof_trail_cmd`` and
``hermes proof create``.
"""

from hermes_cli.local_proof_ops.trail import (
    DEFAULT_PROOF_DIR,
    build_proof_markdown,
    create_proof_record,
    proof_trail_command,
    slugify_title,
)

__all__ = [
    "DEFAULT_PROOF_DIR",
    "build_proof_markdown",
    "create_proof_record",
    "proof_trail_command",
    "slugify_title",
]

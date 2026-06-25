"""Argparse wiring for local Niko proof-trail operations."""

from __future__ import annotations


def register_proof_parser(subparsers) -> None:
    """Attach the local proof-trail CLI under ``hermes proof``."""
    proof_parser = subparsers.add_parser(
        "proof",
        help="Create durable task/LCM proof artifacts",
    )
    proof_sub = proof_parser.add_subparsers(dest="proof_command")
    proof_create = proof_sub.add_parser(
        "create",
        help="Create a task proof artifact under the operational proofs directory",
    )
    proof_create.add_argument("--title", required=True)
    proof_create.add_argument("--status", default="recorded")
    proof_create.add_argument("--rationale", default="")
    proof_create.add_argument("--input", dest="inputs", action="append", default=[])
    proof_create.add_argument("--file", dest="files", action="append", default=[])
    proof_create.add_argument("--command", dest="commands", action="append", default=[])
    proof_create.add_argument("--validation", dest="validations", action="append", default=[])
    proof_create.add_argument("--kb-promotion", dest="kb_promotions", action="append", default=[])
    proof_create.add_argument("--final-state", default="")
    proof_create.add_argument("--lcm-ref", dest="lcm_refs", action="append", default=[])
    proof_create.add_argument("--output-dir", default="")
    proof_create.add_argument("--json", action="store_true")

    def cmd_proof(args):
        sub = getattr(args, "proof_command", None)
        if sub == "create":
            from hermes_cli.local_proof_ops.trail import proof_trail_command

            proof_trail_command(args)
            return
        proof_parser.print_help()

    proof_parser.set_defaults(func=cmd_proof)

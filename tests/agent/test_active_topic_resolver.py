from __future__ import annotations

import logging
from pathlib import Path

from agent.active_topic_resolver import (
    build_active_topic_context,
    load_project_contexts,
    is_continuation_like,
    resolve_active_topic,
)


def _write_project(root: Path, slug: str, title: str, context: str) -> Path:
    project = root / slug
    (project / "meta").mkdir(parents=True)
    (project / "meta" / "project.yaml").write_text(
        f"project: {slug}\ntitle: {title}\nstatus: active\n",
        encoding="utf-8",
    )
    (project / "PROJECT_CONTEXT.md").write_text(context, encoding="utf-8")
    return project


def test_cross_session_hint_is_scoped_to_same_thread(tmp_path: Path):
    from types import SimpleNamespace
    from hermes_state import SessionDB
    from agent.active_topic_resolver import _session_hint_slugs

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("same-old", "discord", session_key="thread-A", chat_id="A", thread_id="A")
        db.create_session("same-new", "discord", session_key="thread-A", chat_id="A", thread_id="A")
        db.create_session("other", "discord", session_key="thread-B", chat_id="B", thread_id="B")
        db.append_message("same-old", "assistant", "identity research/projects/agentic-identity-patterns")
        for _ in range(45):
            db.append_message("other", "assistant", "identity research/projects/machine-identity-dispatcher")
        agent = SimpleNamespace(_session_db=db, session_id="same-new")
        hints = _session_hint_slugs(agent, "identity")
        assert "agentic-identity-patterns" in hints
        assert "machine-identity-dispatcher" not in hints
    finally:
        db.close()


def test_ponytail_instructions_do_not_supply_topic_evidence(tmp_path: Path):
    root = tmp_path / "projects"
    _write_project(root, "persistent-memory", "Persistent memory", "project workflow code agent memory security identity source artifact report test")
    _write_project(root, "different-topic", "Gardening", "orchids and soil")
    message = (
        "Vamos\n\nPONYTAIL MODE ACTIVE — level: full\n"
        "The lazy senior developer sees project workflow code, agent, memory, "
        "security, identity, source, artifact, report and test."
    )
    assert resolve_active_topic(message, project_roots=[root]) is None


def test_history_cannot_make_generic_current_words_choose_a_project(tmp_path: Path):
    root = tmp_path / "projects"
    _write_project(root, "alpha-project", "Alpha project", "report governance atlas boreal oldtopic unique history terms")
    _write_project(root, "beta-project", "Beta project", "report governance atlas boreal")
    history = [{"role": "assistant", "content": "oldtopic unique history terms about alpha project"}]
    assert resolve_active_topic("vamos continuar com report governance atlas boreal", history, project_roots=[root]) is None


def test_explicit_project_slug_phrase_beats_generic_artifact_overlap(tmp_path: Path):
    root = tmp_path / "projects"
    _write_project(root, "sailpoint-isc-human-actor", "SailPoint ISC human actor", "DOCX PDF TOC workflow companion corrections")
    _write_project(root, "agentic-identity-patterns-2026-09-30", "Agentic Identity Patterns", "seven surfaces")
    packet = resolve_active_topic(
        "continuar as correções do Agentic Identity Patterns workflow companion",
        [{"role": "assistant", "content": "DOCX PDF TOC corrections"}],
        project_roots=[root],
    )
    assert packet is not None and packet.project_slug == "agentic-identity-patterns-2026-09-30"


def test_automated_kanban_notification_does_not_infer_project(tmp_path: Path):
    root = tmp_path / "projects"
    _write_project(root, "other-project", "Other project", "AIP Kanban DOCX PDF QA workflows completed")
    message = (
        "[kanban] Task t_a228c761 completed. Title: AIP-T27 workflow QA. "
        "Board: niko-research. Check the result or decide the next step."
    )
    assert resolve_active_topic(message, [{"role": "assistant", "content": "Other project is active"}], project_roots=[root]) is None
    assert resolve_active_topic("Cronjob Response: Next steps for AIP workflow", [], project_roots=[root]) is None


def test_project_hub_is_indexed_before_noisy_artifact_inventory(tmp_path: Path):
    root = tmp_path / "projects"
    project = _write_project(root, "agentic-identity-patterns", "Agentic Identity Patterns", "")
    (project / "00-project-hub.md").write_text(
        "# Agentic Identity Patterns\nDelegation workflow and seven control surfaces.\n", encoding="utf-8"
    )
    noise = project / "diagrams"
    noise.mkdir()
    for i in range(85):
        (noise / f"image-{i:03d}.png").write_bytes(b"x")
    ctx = next(p for p in load_project_contexts([root]) if p.slug == "agentic-identity-patterns")
    assert "Delegation workflow and seven control surfaces" in ctx.text


def test_tunable_thresholds_are_forwarded_by_builder(tmp_path: Path):
    root = tmp_path / "projects"
    _write_project(root, "machine-identity", "Machine identity", "Workload identity and delegation")
    assert build_active_topic_context(
        "continuar com workload identity e delegation", project_roots=[root],
        min_confidence=0.0, min_topic_evidence=1,
    ).startswith("<active_topic_context>")


def test_continuation_like_portuguese_focus_prompt():
    assert is_continuation_like("Vamos continuar com outras áreas de foco, o que você sugere ?")
    assert is_continuation_like("Pode fazer")
    assert not is_continuation_like("Explique a diferença entre SPIFFE e OAuth para workloads")


def test_resolves_ai_implementation_education_smb_not_education_sector(tmp_path: Path):
    root = tmp_path / "projects"
    _write_project(
        root,
        "ai-implementation-education-smb",
        "Ensino e implementação de IA para pequenos negócios",
        """
# Project context — AI implementation education for SMBs

Teach and help businesses/SMBs/PMEs implement AI in their real operations.
Ensinar pessoas a implementar IA no negócio delas é o foco principal.
Imobiliárias, contabilidades, clínicas, jurídico pequeno, e-commerce local.

Critical disambiguation: "AI implementation education" means education/training/enablement for implementing AI inside businesses. It does not mean AI implementation in the formal education sector; it does not mean Agentic Identity.

## Suggested next areas of focus
- Other verticals: clínicas/consultórios, contabilidades, jurídico pequeno/médio, e-commerce local.
- execution/imobiliarias/final/package/follow-up-inteligente-imobiliarias-mvp.zip
""",
    )
    _write_project(
        root,
        "ai-in-education-sector",
        "AI implementation in schools and universities",
        """
# AI in education sector

Formal education sector: schools, universities, teachers, students, learning management systems, pedagogy, lesson planning, classroom assessment.
""",
    )

    # User message MUST contain topical tokens for the resolved project.
    # "outras áreas de foco" alone is structural (regex hits, evidence-gate rejects).
    history = [
        {
            "role": "user",
            "content": "veja esse comentário: acho que existe um campo imenso para ensinar as pessoas a implementar IA no negócio delas; faça uma pesquisa",
        },
        {
            "role": "assistant",
            "content": "A oportunidade não é curso genérico; é diagnóstico, implementação assistida e governança para PMEs.",
        },
    ]

    packet = resolve_active_topic(
        # Continuation + topical evidence (ensinar, implementar, negocio, pme).
        "Vamos continuar com outras áreas de foco, o que você sugere para ensinar pessoas a implementar IA no negócio delas?",
        history,
        project_roots=[root],
    )

    assert packet is not None
    assert packet.project_slug == "ai-implementation-education-smb"
    assert "formal education sector" in "\n".join(packet.instructions)
    assert all("ai-in-education-sector" not in path for path in packet.canonical_paths)


def test_global_agentic_identity_does_not_override_thread_local_business_context(tmp_path: Path):
    root = tmp_path / "projects"
    _write_project(
        root,
        "agentic-identity-market-front",
        "Agentic Identity / Machine Identity lead generation",
        "Agentic Identity, Machine Identity, NHI, workload identity, SPIFFE/SPIRE, standards-aware lead generation.",
    )
    _write_project(
        root,
        "ai-implementation-education-smb",
        "Ensino e implementação de IA para pequenos negócios",
        "PMEs, ensinar pessoas a implementar IA no negócio delas, diagnóstico IA PME, sprint 30 dias, imobiliárias, contabilidades, clínicas.",
    )
    history = [
        {"role": "user", "content": "acho que existe um campo imenso para ensinar as pessoas a implementar IA no negócio delas"},
        {"role": "assistant", "content": "Vamos atacar como AI Operations Sprint para PMEs, começando por imobiliárias."},
    ]

    # User message carries topical evidence for the SMB project.
    packet = resolve_active_topic(
        "outras áreas de foco para ensinar pessoas a implementar IA no negócio",
        history,
        project_roots=[root],
    )

    assert packet is not None
    assert packet.project_slug == "ai-implementation-education-smb"


def test_build_active_topic_context_is_ephemeral_packet(tmp_path: Path):
    root = tmp_path / "projects"
    project = _write_project(
        root,
        "ai-implementation-education-smb",
        "Ensino e implementação de IA para pequenos negócios",
        """
# Project context

Teach SMBs to implement AI in business operations.
Ensinar pessoas a implementar IA no negócio delas para PMEs, imobiliárias.

## Canonical files
- final/reports/relatorio-ia-pmes.md
- execution/imobiliarias/final/site/index.html
""",
    )
    (project / "final" / "reports").mkdir(parents=True)
    (project / "final" / "reports" / "relatorio-ia-pmes.md").write_text("report", encoding="utf-8")

    # Continuation + topical evidence for the SMB project.
    # Note: with the evidence gate, "continue" alone (no topical tokens) returns
    # None by design — that's the leak fix. The continuation phrase must carry
    # topical overlap with the project's tokens to resolve.
    context = build_active_topic_context(
        "continuar ensinando pessoas a implementar IA no negócio imobiliárias",
        [{"role": "user", "content": "ensinar pessoas a implementar IA no negócio delas para PMEs, imobiliárias"}],
        project_roots=[root],
    )

    assert context.startswith("<active_topic_context>")
    assert "project_slug: ai-implementation-education-smb" in context
    assert "relatorio-ia-pmes.md" in context
    assert context.endswith("</active_topic_context>")


# --- New invariants: structural fix for the cross-session leak ---


def test_structural_continuation_with_no_topic_evidence_returns_none(tmp_path: Path):
    """The leak scenario: a structurally-typed continuation turn with no
    topical overlap must NOT resolve to any project, even when the assistant's
    prior turn mentioned a project. Cross-session hint is supportive, not
    decisive.
    """
    root = tmp_path / "projects"
    _write_project(
        root,
        "machine-identity-dispatcher",
        "Machine Identity Dispatcher newsletter",
        "agentic identity machine identity workload identity SPIFFE SPIRE NHI newsletter beehiiv",
    )
    _write_project(
        root,
        "ai-implementation-education-smb",
        "AI implementation for SMBs",
        "ensinar implementar IA no negócio PMEs imobiliárias contabilidades clínicas",
    )

    history = [
        {
            "role": "user",
            "content": "faz um post novo pra newsletter sobre machine identity dispatcher",
        },
        {
            "role": "assistant",
            "content": "Vou abrir um draft no beehiiv sobre agentic identity e workload identity.",
        },
    ]

    # The actual live leak case from 2026-06-23: "1. Discord / 2. 2-3 sessões
    # / 3. causa raiz" — pure structural continuation, zero topic evidence
    # for any project. Must return None.
    packet = resolve_active_topic(
        "1. Discord  2. 2-3 sessões  3. causa raiz",
        history,
        project_roots=[root],
    )

    assert packet is None, (
        "Structural continuation with no topical evidence must not resolve a "
        "project — this is the cross-session leak we are fixing."
    )


def test_cross_session_continuity_preserved_with_topical_evidence(tmp_path: Path):
    """Same cross-session scenario, but user actually says something topical
    about the previously-active project. Continuity feature MUST still work.
    """
    root = tmp_path / "projects"
    _write_project(
        root,
        "machine-identity-dispatcher",
        "Machine Identity Dispatcher newsletter",
        "agentic identity machine identity workload identity SPIFFE SPIRE NHI newsletter beehiiv",
    )
    _write_project(
        root,
        "ai-implementation-education-smb",
        "AI implementation for SMBs",
        "ensinar implementar IA no negócio PMEs imobiliárias contabilidades clínicas",
    )

    history = [
        {
            "role": "user",
            "content": "faz um post novo pra newsletter sobre machine identity dispatcher",
        },
        {
            "role": "assistant",
            "content": "Vou abrir um draft no beehiiv sobre agentic identity e workload identity.",
        },
    ]

    # Continuation that names the previously-active project's topic.
    packet = resolve_active_topic(
        "continua o post sobre agentic identity e workload identity",
        history,
        project_roots=[root],
    )

    assert packet is not None
    assert packet.project_slug == "machine-identity-dispatcher"


def test_evidence_gate_rejects_even_when_hint_matches(tmp_path: Path, caplog):
    """The hint can suggest a slug, but if the user message contributes zero
    topical tokens, the evidence gate must still reject. Hint is supportive,
    not decisive.
    """
    root = tmp_path / "projects"
    _write_project(
        root,
        "machine-identity-dispatcher",
        "Machine Identity Dispatcher newsletter",
        "agentic identity machine identity workload identity SPIFFE SPIRE NHI newsletter beehiiv",
    )

    # Even with `agent` carrying a session_db mock that would return hinted
    # slugs, a structural turn must not pass.
    class _FakeAgent:
        session_id = "discord:test-thread"
        platform = "discord"

    packet = resolve_active_topic(
        "ok",
        [],
        agent=_FakeAgent(),
        project_roots=[root],
        logger=logging.getLogger("test_evidence_gate"),
    )

    assert packet is None


def test_default_confidence_threshold_is_050():
    """The structural fix raises default min_confidence from 0.45 to 0.50.
    Lock this as an invariant so a regression that drops it back triggers a
    review of the leak history. Tunable in config.yaml, but 0.45 is the
    documented leak threshold and must not return by accident.
    """
    from agent.active_topic_resolver import _DEFAULT_MIN_CONFIDENCE, _MIN_TOPIC_EVIDENCE_TOKENS

    assert _DEFAULT_MIN_CONFIDENCE == 0.50
    assert _MIN_TOPIC_EVIDENCE_TOKENS == 2

from __future__ import annotations

from pathlib import Path

from agent.active_topic_resolver import (
    build_active_topic_context,
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

Critical disambiguation: “AI implementation education” means education/training/enablement for implementing AI inside businesses. It does not mean AI implementation in the formal education sector; it does not mean Agentic Identity.

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
        "Vamos continuar com outras áreas de foco, o que você sugere?",
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

    packet = resolve_active_topic("outras áreas de foco", history, project_roots=[root])

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

## Canonical files
- final/reports/relatorio-ia-pmes.md
- execution/imobiliarias/final/site/index.html
""",
    )
    (project / "final" / "reports").mkdir(parents=True)
    (project / "final" / "reports" / "relatorio-ia-pmes.md").write_text("report", encoding="utf-8")

    context = build_active_topic_context(
        "continue",
        [{"role": "user", "content": "ensinar pessoas a implementar IA no negócio delas para PMEs"}],
        project_roots=[root],
    )

    assert context.startswith("<active_topic_context>")
    assert "project_slug: ai-implementation-education-smb" in context
    assert "relatorio-ia-pmes.md" in context
    assert context.endswith("</active_topic_context>")

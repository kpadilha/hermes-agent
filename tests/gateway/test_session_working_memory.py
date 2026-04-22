from datetime import datetime

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionEntry, SessionSource, SessionStore


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="96809052",
        chat_type="dm",
        user_id="96809052",
        user_name="Krishna",
    )


def test_session_entry_round_trips_working_memory():
    entry = SessionEntry(
        session_key="agent:main:telegram:dm:96809052",
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        working_memory={
            "last_assistant_actionable": "Desenhar a diferença central entre Kami e Hermes/Niko.",
            "awaiting_confirmation": True,
            "active_topic": "kami-vs-hermes-document-pipeline",
        },
    )

    restored = SessionEntry.from_dict(entry.to_dict())

    assert restored.working_memory["awaiting_confirmation"] is True
    assert restored.working_memory["active_topic"] == "kami-vs-hermes-document-pipeline"


def test_working_memory_round_trips_proposal_ids(tmp_path):
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    source = _make_source()
    entry = store.get_or_create_session(source)

    wm = {
        "last_assistant_actionable": "Desenhar a diferença central entre Kami e Hermes/Niko.",
        "awaiting_confirmation": True,
        "pending_proposals": [
            {"proposal_id": "p1", "text": "Desenhar a diferença central entre Kami e Hermes/Niko."},
            {"proposal_id": "p2", "text": "Gerar uma tabela comparativa."},
        ],
        "ambiguity_requires_clarification": True,
    }
    store.update_session(entry.session_key, working_memory=wm)

    reloaded = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    same_entry = reloaded.get_or_create_session(source)

    assert same_entry.working_memory["pending_proposals"][1]["proposal_id"] == "p2"

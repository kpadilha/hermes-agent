from run_agent import AIAgent


def test_is_short_confirmation_message_positive_cases():
    assert AIAgent._is_short_confirmation_message("sim") is True
    assert AIAgent._is_short_confirmation_message("SIM dwsenhe") is True
    assert AIAgent._is_short_confirmation_message("continue") is True
    assert AIAgent._is_short_confirmation_message("pode seguir") is True


def test_is_short_confirmation_message_negative_cases():
    assert AIAgent._is_short_confirmation_message("") is False
    assert AIAgent._is_short_confirmation_message("quero um resumo detalhado do pipeline") is False
    assert AIAgent._is_short_confirmation_message("discordo da análise") is False


def test_extract_recent_assistant_actionable_prefers_latest_assistant_message():
    history = [
        {"role": "assistant", "content": "Posso desenhar o fluxo antigo."},
        {"role": "user", "content": "não"},
        {"role": "assistant", "content": "Fiz a comparação aplicada ao teu stack. Se quiser, eu desenho a diferença central entre Kami e Hermes/Niko document pipeline."},
    ]

    result = AIAgent._extract_recent_assistant_actionable(history)

    assert result == "Fiz a comparação aplicada ao teu stack. Se quiser, eu desenho a diferença central entre Kami e Hermes/Niko document pipeline."


def test_extract_recent_assistant_actionable_ignores_non_assistant_roles():
    history = [
        {"role": "tool", "content": "tool output"},
        {"role": "user", "content": "sim"},
    ]

    assert AIAgent._extract_recent_assistant_actionable(history) is None


def test_extract_recent_assistant_actionables_returns_multiple_recent_candidates():
    history = [
        {"role": "assistant", "content": "Posso desenhar o fluxo antigo."},
        {"role": "assistant", "content": "Também posso gerar uma tabela comparativa."},
    ]

    result = AIAgent._extract_recent_assistant_actionables(history, limit=3)

    assert [item["text"] for item in result] == [
        "Também posso gerar uma tabela comparativa.",
        "Posso desenhar o fluxo antigo.",
    ]
    assert result[0]["proposal_id"].startswith("p")
    assert result[0]["source_message_index"] == 1
    assert result[0]["lifecycle_version"] == 1


def test_extract_recent_assistant_actionables_assigns_distinct_ids_to_repeated_text_at_different_turns():
    history = [
        {"role": "assistant", "content": "Posso desenhar o fluxo antigo."},
        {"role": "user", "content": "não agora"},
        {"role": "assistant", "content": "Posso desenhar o fluxo antigo."},
    ]

    result = AIAgent._extract_recent_assistant_actionables(history, limit=3)

    assert len(result) == 2
    assert result[0]["text"] == result[1]["text"]
    assert result[0]["proposal_id"] != result[1]["proposal_id"]
    assert result[0]["source_message_index"] == 2
    assert result[1]["source_message_index"] == 0
    assert result[0]["lifecycle_version"] == 2
    assert result[1]["lifecycle_version"] == 1


def test_apply_recent_turn_binding_injects_continuity_hint_for_short_confirmation():
    history = [
        {"role": "assistant", "content": "Fiz a comparação aplicada ao teu stack. Se quiser, eu desenho a diferença central entre Kami e Hermes/Niko document pipeline."}
    ]

    bound = AIAgent._apply_recent_turn_binding("SIM dwsenhe", history)

    assert "SIM dwsenhe" in bound
    assert "immediately preceding assistant proposal" in bound
    assert "Kami e Hermes/Niko document pipeline" in bound


def test_apply_recent_turn_binding_prefers_session_working_memory_over_history():
    history = [
        {"role": "assistant", "content": "Posso desenhar o fluxo antigo."}
    ]
    working_memory = {
        "last_assistant_actionable": "Desenhar a diferença central entre Kami e Hermes/Niko document pipeline.",
        "awaiting_confirmation": True,
        "active_topic": "kami-vs-hermes-document-pipeline",
    }

    bound = AIAgent._apply_recent_turn_binding("sim", history, working_memory)

    assert "Kami e Hermes/Niko document pipeline" in bound
    assert "fluxo antigo" not in bound


def test_apply_recent_turn_binding_blocks_automatic_binding_when_working_memory_is_ambiguous():
    history = [{"role": "assistant", "content": "Posso desenhar o fluxo antigo."}]
    working_memory = {
        "last_assistant_actionable": "Desenhar a diferença central entre Kami e Hermes/Niko document pipeline.",
        "awaiting_confirmation": True,
        "active_topic": "kami-vs-hermes-document-pipeline",
        "pending_proposals": [
            "Desenhar a diferença central entre Kami e Hermes/Niko document pipeline.",
            "Gerar uma tabela comparativa entre Kami e Hermes/Niko.",
        ],
        "ambiguity_requires_clarification": True,
    }

    assert AIAgent._apply_recent_turn_binding("sim", history, working_memory) == "sim"


def test_build_recent_turn_working_memory_marks_ambiguity_for_multiple_recent_proposals():
    history = [
        {"role": "assistant", "content": "Posso desenhar o fluxo antigo."},
        {"role": "assistant", "content": "Também posso gerar uma tabela comparativa."},
    ]

    wm = AIAgent._build_recent_turn_working_memory(history)

    assert wm["ambiguity_requires_clarification"] is True
    assert len(wm["pending_proposals"]) == 2
    assert wm["pending_proposals"][0]["proposal_id"].startswith("p")
    assert wm["pending_proposals"][0]["source_message_index"] == 1


def test_normalize_pending_proposals_preserves_lifecycle_metadata():
    proposals = [{
        "proposal_id": "p123",
        "text": "Gerar uma tabela comparativa.",
        "source_message_index": 7,
        "lifecycle_version": 3,
    }]

    normalized = AIAgent._normalize_pending_proposals(proposals)

    assert normalized == proposals


def test_apply_user_message_to_working_memory_consumes_unambiguous_confirmation():
    working_memory = {
        "last_assistant_actionable": "Desenhar a diferença central entre Kami e Hermes/Niko.",
        "awaiting_confirmation": True,
        "pending_proposals": ["Desenhar a diferença central entre Kami e Hermes/Niko."],
        "ambiguity_requires_clarification": False,
    }

    updated = AIAgent._apply_user_message_to_working_memory("sim", working_memory)

    assert updated == {}


def test_apply_user_message_to_working_memory_keeps_ambiguous_state_until_disambiguated():
    working_memory = {
        "last_assistant_actionable": "Desenhar a diferença central entre Kami e Hermes/Niko.",
        "awaiting_confirmation": True,
        "pending_proposals": [
            "Desenhar a diferença central entre Kami e Hermes/Niko.",
            "Gerar uma tabela comparativa.",
        ],
        "ambiguity_requires_clarification": True,
    }

    updated = AIAgent._apply_user_message_to_working_memory("sim", working_memory)

    assert updated == working_memory


def test_build_disambiguation_prompt_lists_pending_proposals():
    working_memory = {
        "pending_proposals": [
            "Desenhar a diferença central entre Kami e Hermes/Niko.",
            "Gerar uma tabela comparativa.",
        ],
        "ambiguity_requires_clarification": True,
    }

    prompt = AIAgent._build_disambiguation_prompt(working_memory)

    assert "Qual opção você quer" in prompt
    assert "1." in prompt
    assert "2." in prompt


def test_maybe_build_disambiguation_response_returns_prompt_for_ambiguous_short_confirmation():
    working_memory = {
        "pending_proposals": [
            "Desenhar a diferença central entre Kami e Hermes/Niko.",
            "Gerar uma tabela comparativa.",
        ],
        "ambiguity_requires_clarification": True,
    }

    response = AIAgent._maybe_build_disambiguation_response("sim", working_memory)

    assert response is not None
    assert "Qual opção você quer" in response


def test_maybe_build_disambiguation_response_returns_none_when_not_ambiguous():
    working_memory = {
        "pending_proposals": ["Desenhar a diferença central entre Kami e Hermes/Niko."],
        "ambiguity_requires_clarification": False,
    }

    assert AIAgent._maybe_build_disambiguation_response("sim", working_memory) is None


def test_resolve_proposal_selection_by_number():
    working_memory = {
        "pending_proposals": [
            {"proposal_id": "p1", "text": "Desenhar a diferença central entre Kami e Hermes/Niko."},
            {"proposal_id": "p2", "text": "Gerar uma tabela comparativa."},
        ],
        "ambiguity_requires_clarification": True,
    }

    resolved = AIAgent._resolve_proposal_selection("2", working_memory)
    assert resolved is not None
    assert resolved["text"] == "Gerar uma tabela comparativa."
    assert resolved["proposal_id"] == "p2"


def test_resolve_proposal_selection_by_ordinal_word():
    working_memory = {
        "pending_proposals": [
            {"proposal_id": "p1", "text": "Desenhar a diferença central entre Kami e Hermes/Niko."},
            {"proposal_id": "p2", "text": "Gerar uma tabela comparativa."},
        ],
        "ambiguity_requires_clarification": True,
    }

    resolved = AIAgent._resolve_proposal_selection("segunda", working_memory)
    assert resolved is not None
    assert resolved["proposal_id"] == "p2"


def test_resolve_proposal_selection_by_label_match():
    working_memory = {
        "pending_proposals": [
            {"proposal_id": "p1", "text": "Desenhar o diagrama da diferença central."},
            {"proposal_id": "p2", "text": "Gerar uma tabela comparativa."},
        ],
        "ambiguity_requires_clarification": True,
    }

    resolved = AIAgent._resolve_proposal_selection("o diagrama", working_memory)
    assert resolved is not None
    assert resolved["proposal_id"] == "p1"


def test_resolve_proposal_selection_by_explicit_proposal_id():
    working_memory = {
        "pending_proposals": [
            {"proposal_id": "pabc12345", "text": "Desenhar o diagrama da diferença central."},
            {"proposal_id": "pdef67890", "text": "Gerar uma tabela comparativa."},
        ],
        "ambiguity_requires_clarification": True,
    }

    resolved = AIAgent._resolve_proposal_selection("quero a pdef67890", working_memory)
    assert resolved is not None
    assert resolved["proposal_id"] == "pdef67890"


def test_run_conversation_returns_disambiguation_prompt_without_model_call():
    agent = AIAgent(model="test", api_key="x", base_url="http://example.com/v1", session_working_memory={
        "pending_proposals": [
            {"proposal_id": "p1", "text": "Desenhar a diferença central entre Kami e Hermes/Niko."},
            {"proposal_id": "p2", "text": "Gerar uma tabela comparativa."},
        ],
        "ambiguity_requires_clarification": True,
        "awaiting_confirmation": True,
    }, enabled_toolsets=[])

    result = agent.run_conversation("sim", conversation_history=[])

    assert result["needs_disambiguation"] is True
    assert "Qual opção você quer" in result["final_response"]
    assert result["api_calls"] == 0


def test_merge_recent_turn_working_memory_clears_consumed_proposal_when_no_new_one_is_created():
    prior = {
        "last_assistant_actionable": "Desenhar a diferença central entre Kami e Hermes/Niko.",
        "awaiting_confirmation": True,
        "pending_proposals": ["Desenhar a diferença central entre Kami e Hermes/Niko."],
        "ambiguity_requires_clarification": False,
    }
    messages = [
        {"role": "user", "content": "sim"},
        {"role": "assistant", "content": "Aqui está o diagrama."},
    ]

    merged = AIAgent._merge_recent_turn_working_memory(prior, "sim", messages)

    assert merged == {}


def test_merge_recent_turn_working_memory_resolves_ambiguous_selection_by_number():
    prior = {
        "last_assistant_actionable": "Desenhar a diferença central entre Kami e Hermes/Niko.",
        "awaiting_confirmation": True,
        "pending_proposals": [
            {"proposal_id": "p1", "text": "Desenhar a diferença central entre Kami e Hermes/Niko."},
            {"proposal_id": "p2", "text": "Gerar uma tabela comparativa."},
        ],
        "ambiguity_requires_clarification": True,
    }
    messages = [
        {"role": "user", "content": "2"},
        {"role": "assistant", "content": "Aqui está a tabela comparativa."},
    ]

    merged = AIAgent._merge_recent_turn_working_memory(prior, "2", messages)

    assert merged == {}


def test_apply_recent_turn_binding_returns_original_without_actionable_context():
    history = [{"role": "assistant", "content": "Certo."}]

    assert AIAgent._apply_recent_turn_binding("sim", history) == "sim"


def test_apply_recent_turn_binding_does_not_touch_non_confirmation_message():
    history = [
        {"role": "assistant", "content": "Se quiser, eu desenho isso agora."}
    ]

    original = "desenhe a arquitetura completa com 3 camadas"
    assert AIAgent._apply_recent_turn_binding(original, history) == original

"""Tests for deterministic relevant-context pin selection."""

from agent.context_relevance import (
    RelevantContextPin,
    _extract_query_terms,
    select_relevant_context_pins,
)


def test_extract_query_terms_keeps_paths_errors_and_focus_terms():
    terms = _extract_query_terms(
        "Investigate HTTP 401 primary_auth_expiry in agent/context_compressor.py and TypeError",
        focus_topic="Codex OAuth renewal",
        tail_text="recent tail mentions tests/run_agent/test_compression.py",
    )

    assert "agent/context_compressor.py" in terms.exact
    assert "tests/run_agent/test_compression.py" in terms.exact
    assert "primary_auth_expiry" in terms.exact
    assert "HTTP 401" in terms.phrases
    assert "#10896" in _extract_query_terms('see #10896 and "foo bar baz"').exact
    assert "foo bar baz" in _extract_query_terms('see #10896 and "foo bar baz"').phrases
    assert "TypeError" in terms.exact
    assert "codex" in terms.words
    assert "oauth" in terms.words
    assert "in" not in terms.words


def test_select_relevant_context_pins_prioritizes_exact_path_and_error_matches():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "Unrelated weather question."},
        {"role": "assistant", "content": "The sky is clear."},
        {"role": "user", "content": "Decision: agent/context_compressor.py must preserve primary_auth_expiry details."},
        {"role": "assistant", "content": "Noted the root cause and remaining work."},
        {"role": "user", "content": "Latest ask: what happened with primary_auth_expiry in agent/context_compressor.py?"},
    ]

    pins = select_relevant_context_pins(
        messages,
        candidate_start=1,
        candidate_end=5,
        focus_topic=None,
        max_pins=3,
        max_chars_total=2000,
        min_score=3,
    )

    assert pins
    assert isinstance(pins[0], RelevantContextPin)
    assert pins[0].index == 3
    assert pins[0].score > 3
    assert "agent/context_compressor.py" in pins[0].excerpt
    assert "primary_auth_expiry" in pins[0].excerpt
    assert "path" in pins[0].reason or "exact" in pins[0].reason


def test_select_relevant_context_pins_skips_context_summaries_and_caps_output():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] agent/context_compressor.py old summary"},
        {"role": "user", "content": "agent/context_compressor.py " + "x" * 2000},
        {"role": "assistant", "content": "agent/context_compressor.py another relevant mention"},
        {"role": "user", "content": "Latest: agent/context_compressor.py"},
    ]

    pins = select_relevant_context_pins(
        messages,
        candidate_start=1,
        candidate_end=4,
        max_pins=5,
        max_chars_total=600,
        min_score=1,
    )

    assert pins
    assert all("CONTEXT COMPACTION" not in pin.excerpt for pin in pins)
    assert sum(len(pin.excerpt) for pin in pins) <= 600

    tiny = select_relevant_context_pins(
        messages,
        candidate_start=1,
        candidate_end=4,
        max_pins=5,
        max_chars_total=5,
        min_score=1,
    )
    assert sum(len(pin.excerpt) for pin in tiny) <= 5


def test_compact_item_reference_selects_each_numbered_item():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "Item 1: add lexical relevance pins."},
        {"role": "assistant", "content": "Item 1 recorded."},
        {"role": "user", "content": "Item 2: keep pins reference-only."},
        {"role": "assistant", "content": "Item 2 recorded."},
        {"role": "user", "content": "Item 3: unrelated future embeddings."},
        {"role": "user", "content": "Latest: vamos com itens 1 e 2."},
    ]

    pins = select_relevant_context_pins(
        messages,
        candidate_start=1,
        candidate_end=6,
        max_pins=8,
        min_score=3,
    )
    rendered = "\n".join(pin.excerpt for pin in pins)

    assert "Item 1" in rendered
    assert "Item 2" in rendered
    assert "Item 3" not in rendered


def test_select_relevant_context_pins_does_not_select_standalone_tool_result():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "I will inspect the file", "tool_calls": [{"id": "call_1", "function": {"name": "read_file", "arguments": "{\"path\": \"agent/context_compressor.py\"}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "agent/context_compressor.py huge tool result with primary_auth_expiry"},
        {"role": "user", "content": "Latest: primary_auth_expiry in agent/context_compressor.py"},
    ]

    pins = select_relevant_context_pins(
        messages,
        candidate_start=1,
        candidate_end=3,
        max_pins=5,
        min_score=1,
    )

    assert pins
    assert all(pin.role != "tool" for pin in pins)

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from run_agent import AIAgent
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner(history: list[dict[str, str]]):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = history
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._handle_message = AsyncMock(return_value="retried")
    return runner, session_entry


@pytest.mark.asyncio
async def test_retry_rebuilds_working_memory_after_transcript_truncation():
    history = [
        {"role": "assistant", "content": "Posso explicar o fluxo atual."},
        {"role": "user", "content": "faça"},
        {"role": "assistant", "content": "Aqui está o fluxo."},
        {"role": "user", "content": "ajuste isso"},
        {"role": "assistant", "content": "Ajustei."},
    ]
    runner, session_entry = _make_runner(history)
    expected_truncated = history[:3]
    expected_working_memory = AIAgent._build_recent_turn_working_memory(expected_truncated)

    with patch("run_agent.AIAgent") as agent_cls:
        agent_cls._build_recent_turn_working_memory.return_value = expected_working_memory
        result = await runner._handle_retry_command(_make_event("/retry"))

    assert result == "retried"
    runner.session_store.rewrite_transcript.assert_called_once_with(session_entry.session_id, expected_truncated)
    runner.session_store.update_session.assert_called_once_with(
        session_entry.session_key,
        last_prompt_tokens=0,
        working_memory=expected_working_memory,
    )
    assert session_entry.working_memory == expected_working_memory
    retried_event = runner._handle_message.await_args.args[0]
    assert retried_event.text == "ajuste isso"


@pytest.mark.asyncio
async def test_undo_rebuilds_working_memory_after_transcript_truncation():
    history = [
        {"role": "assistant", "content": "Posso explicar o fluxo atual."},
        {"role": "user", "content": "faça"},
        {"role": "assistant", "content": "Aqui está o fluxo."},
        {"role": "user", "content": "ajuste isso"},
        {"role": "assistant", "content": "Ajustei."},
    ]
    runner, session_entry = _make_runner(history)
    expected_truncated = history[:3]
    expected_working_memory = AIAgent._build_recent_turn_working_memory(expected_truncated)

    with patch("run_agent.AIAgent") as agent_cls:
        agent_cls._build_recent_turn_working_memory.return_value = expected_working_memory
        result = await runner._handle_undo_command(_make_event("/undo"))

    assert "Undid 2 message(s)" in result
    runner.session_store.rewrite_transcript.assert_called_once_with(session_entry.session_id, expected_truncated)
    runner.session_store.update_session.assert_called_once_with(
        session_entry.session_key,
        last_prompt_tokens=0,
        working_memory=expected_working_memory,
    )
    assert session_entry.working_memory == expected_working_memory

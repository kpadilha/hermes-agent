"""Gateway /kanban create delegation contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="user-1",
            chat_id="chat-1",
            chat_type="dm",
            thread_id="topic-1",
        ),
    )


def _runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = SimpleNamespace(platforms={})
    runner._kanban_notifier_profile = "gateway-profile"
    runner._active_profile_name = lambda: "gateway-profile"
    return runner


@pytest.mark.asyncio
async def test_kanban_create_slash_does_not_write_subscription_in_gateway(monkeypatch):
    """The shared CLI run_slash path owns create subscriptions, not gateway."""
    import hermes_cli.kanban as kanban_cli
    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(
        kanban_cli,
        "run_slash",
        lambda _text: "Created t_1234abcd  (ready, assignee=worker)",
    )

    def forbidden_add_notify_sub(*_args, **_kwargs):
        raise AssertionError("gateway must not subscribe separately")

    monkeypatch.setattr(kb, "add_notify_sub", forbidden_add_notify_sub)

    out = await _runner()._handle_kanban_command(_event("/kanban create demo"))

    assert out == "Created t_1234abcd  (ready, assignee=worker)"

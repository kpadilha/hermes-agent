"""Discord approval UX regressions.

Pins Discord API-size safety for approval prompts so a long security reason
cannot break button delivery and silently fall back into a poor text flow.
"""

from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


class RecordingChannel:
    def __init__(self):
        self.calls = []

    async def send(self, **kwargs):
        embed = kwargs.get("embed")
        if embed is not None:
            assert len(embed.description or "") <= 4096
            for field in embed.fields:
                assert len(field["name"] or "") <= 256
                assert len(field["value"] or "") <= 1024
        self.calls.append(kwargs)
        return SimpleNamespace(id=12345, embeds=[embed], edit=lambda **_: None)


@pytest.mark.asyncio
async def test_send_exec_approval_truncates_reason_field_to_discord_limit():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    channel = RecordingChannel()
    adapter._client = SimpleNamespace(
        get_channel=lambda _cid: channel,
        fetch_channel=None,
    )
    adapter._allowed_user_ids = {"111"}
    adapter._allowed_role_ids = set()

    result = await adapter.send_exec_approval(
        chat_id="123",
        command="rm -rf /tmp/demo",
        session_key="sess-1",
        description="Security scan — " + ("risky detail " * 200),
    )

    assert result.success is True
    embed = channel.calls[0]["embed"]
    reason = next(f for f in embed.fields if f["name"] == "Reason")
    assert len(reason["value"]) <= 1024
    assert reason["value"].endswith("...")


@pytest.mark.asyncio
async def test_send_exec_approval_keeps_command_embed_description_under_limit():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    channel = RecordingChannel()
    adapter._client = SimpleNamespace(
        get_channel=lambda _cid: channel,
        fetch_channel=None,
    )
    adapter._allowed_user_ids = {"111"}
    adapter._allowed_role_ids = set()

    result = await adapter.send_exec_approval(
        chat_id="123",
        command="x" * 10000,
        session_key="sess-1",
        description="dangerous command",
    )

    assert result.success is True
    embed = channel.calls[0]["embed"]
    assert len(embed.description) <= 4096
    assert embed.description.endswith("...\n```")

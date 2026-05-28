"""Tests for optional Discord thread auto-naming."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import (
    DiscordAdapter,
    _discord_thread_title_from_messages,
    _discord_thread_title_looks_auto_generated,
)


class _FakeThread:
    def __init__(self, name: str, parent_id: int = 123):
        self.id = 456
        self.parent_id = parent_id
        self.name = name
        self.edits: list[str] = []

    async def edit(self, *, name: str, reason: str | None = None):
        self.edits.append(name)
        self.name = name


class _FakeAuthor:
    bot = False


class _FakeMessage:
    def __init__(self, channel: _FakeThread, content: str):
        self.channel = channel
        self.content = content
        self.author = _FakeAuthor()


def _adapter(extra: dict | None = None) -> DiscordAdapter:
    return DiscordAdapter(PlatformConfig(enabled=True, token="token", extra=extra or {}))


def _load_gateway_config_from_dict(yaml_dict: dict):
    from gateway.config import load_gateway_config

    fake_home = Path("/tmp/fake_hermes_home_discord_autoname")

    def fake_exists(self):
        return str(self).endswith("config.yaml")

    with patch("gateway.config.get_hermes_home", return_value=fake_home), \
         patch.object(Path, "exists", fake_exists), \
         patch("builtins.open", create=True) as mock_file:
        mock_file.return_value.__enter__ = lambda s: s
        mock_file.return_value.__exit__ = MagicMock(return_value=False)
        with patch("yaml.safe_load", return_value=yaml_dict):
            return load_gateway_config()


def test_thread_title_detector_flags_truncated_initial_message_titles():
    assert _discord_thread_title_looks_auto_generated(
        "eu vi que o parity guard das 22 falho DE NOVO! O que aconteceu dessa vez? Ess..."
    ) == (True, "truncated")


def test_thread_title_detector_flags_unicode_truncation_markers():
    assert _discord_thread_title_looks_auto_generated(
        "Investigate why Discord forum thread titles lose their final wor…"
    ) == (True, "truncated")


def test_thread_title_detector_flags_discord_max_length_partial_titles():
    partial_title = "Investigate Discord forum title truncation around the eighty character limit saf"
    assert len(partial_title) == 80
    assert _discord_thread_title_looks_auto_generated(partial_title) == (True, "too_long")


def test_thread_title_detector_preserves_specific_human_titles():
    assert _discord_thread_title_looks_auto_generated("Falha recorrente do parity guard") == (
        False,
        "specific_enough",
    )


def test_thread_title_from_messages_removes_conversational_prefix():
    title = _discord_thread_title_from_messages(
        ["eu vi que o parity guard das 22 falho DE NOVO! O que aconteceu dessa vez?"],
        max_length=80,
    )
    assert title == "Parity guard das 22 falho DE NOVO"


def test_thread_title_from_messages_removes_mentions_urls_and_markdown():
    title = _discord_thread_title_from_messages(
        ["<@123> podemos melhorar o **output** do cron no Discord? https://example.com/x"],
        max_length=80,
    )
    assert "<@" not in title
    assert "http" not in title
    assert title == "Melhorar o output do cron no Discord"


def test_top_level_discord_config_bridges_auto_rename_threads():
    cfg = _load_gateway_config_from_dict({
        "discord": {
            "enabled": True,
            "auto_rename_threads": {"enabled": True, "max_length": 64},
        }
    })
    from gateway.config import Platform
    discord_cfg = cfg.platforms[Platform.DISCORD]
    assert discord_cfg.extra["auto_rename_threads"] == {"enabled": True, "max_length": 64}


@pytest.mark.asyncio
async def test_auto_rename_is_disabled_by_default():
    adapter = _adapter()
    thread = _FakeThread("nova thread")
    renamed = await adapter._maybe_auto_rename_thread(_FakeMessage(thread, "parity guard falhou"))
    assert renamed is False
    assert thread.edits == []


@pytest.mark.asyncio
async def test_auto_rename_updates_generic_thread_title_when_enabled():
    adapter = _adapter({"auto_rename_threads": {"enabled": True}})
    thread = _FakeThread("eu vi que o parity guard das 22 falho DE NOVO! O que aconteceu dessa vez? Ess...")
    renamed = await adapter._maybe_auto_rename_thread(
        _FakeMessage(thread, "eu vi que o parity guard das 22 falho DE NOVO! O que aconteceu dessa vez?")
    )
    assert renamed is True
    assert thread.edits == ["Parity guard das 22 falho DE NOVO"]


@pytest.mark.asyncio
async def test_auto_rename_does_not_overwrite_specific_human_title():
    adapter = _adapter({"auto_rename_threads": {"enabled": True}})
    thread = _FakeThread("Falha recorrente do parity guard")
    renamed = await adapter._maybe_auto_rename_thread(_FakeMessage(thread, "parity guard falhou de novo"))
    assert renamed is False
    assert thread.edits == []


@pytest.mark.asyncio
async def test_auto_rename_permission_failure_is_non_fatal(caplog):
    class FailingThread(_FakeThread):
        async def edit(self, *, name: str, reason: str | None = None):
            raise RuntimeError("403 Forbidden: Missing Permissions")

    adapter = _adapter({"auto_rename_threads": {"enabled": True}})
    renamed = await adapter._maybe_auto_rename_thread(_FakeMessage(FailingThread("nova thread"), "parity guard falhou"))
    assert renamed is False
    assert "Failed to auto-rename Discord thread" in caplog.text

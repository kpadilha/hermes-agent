import pytest

from gateway.config import GatewayConfig, Platform
from gateway.delivery import DeliveryRouter, DeliveryTarget, MAX_PLATFORM_OUTPUT, TRUNCATED_VISIBLE


class FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, metadata=None):
        self.sent.append((chat_id, content, metadata or {}))
        return {"ok": True, "content_len": len(content)}


@pytest.mark.asyncio
async def test_platform_delivery_allows_raised_output_window(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = FakeAdapter()
    router = DeliveryRouter(GatewayConfig(), {Platform.DISCORD: adapter})

    content = "x" * MAX_PLATFORM_OUTPUT
    result = await router._deliver_to_platform(
        DeliveryTarget(platform=Platform.DISCORD, chat_id="channel"),
        content,
        {"job_id": "job-1"},
    )

    assert result["content_len"] == MAX_PLATFORM_OUTPUT
    assert adapter.sent[0][1] == content
    assert list((tmp_path / "cron" / "output").glob("*.txt")) == []


@pytest.mark.asyncio
async def test_platform_delivery_truncates_above_raised_output_window(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = FakeAdapter()
    router = DeliveryRouter(GatewayConfig(), {Platform.DISCORD: adapter})

    await router._deliver_to_platform(
        DeliveryTarget(platform=Platform.DISCORD, chat_id="channel"),
        "x" * (MAX_PLATFORM_OUTPUT + 1),
        {"job_id": "job-2"},
    )

    sent = adapter.sent[0][1]
    assert len(sent) > TRUNCATED_VISIBLE
    assert sent.startswith("x" * TRUNCATED_VISIBLE)
    assert "truncated, full output saved" in sent
    saved = list((tmp_path / "cron" / "output").glob("job-2_*.txt"))
    assert len(saved) == 1
    assert saved[0].read_text() == "x" * (MAX_PLATFORM_OUTPUT + 1)

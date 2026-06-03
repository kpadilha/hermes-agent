from gateway import run as gateway_run


def _runner():
    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._restart_requested = False
    runner._draining = False
    runner._session_run_generation = {}
    return runner


def test_update_runtime_status_publishes_active_agent_sessions(monkeypatch):
    calls = []

    def fake_write_runtime_status(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("gateway.status.write_runtime_status", fake_write_runtime_status)
    runner = _runner()
    runner._running_agents["discord:chat:thread:user"] = object()

    runner._update_runtime_status("running")

    assert calls
    assert calls[-1]["gateway_state"] == "running"
    assert calls[-1]["active_agents"] == 1
    assert calls[-1]["active_agent_sessions"] == ["discord:chat:thread:user"]
    assert calls[-1]["activity_status_version"] == 1


def test_release_running_agent_state_updates_runtime_status(monkeypatch):
    calls = []

    def fake_write_runtime_status(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("gateway.status.write_runtime_status", fake_write_runtime_status)
    runner = _runner()
    runner._running_agents["telegram:chat:user"] = object()
    runner._running_agents_ts["telegram:chat:user"] = 123.0

    assert runner._release_running_agent_state("telegram:chat:user") is True

    assert "telegram:chat:user" not in runner._running_agents
    assert calls[-1]["active_agents"] == 0
    assert calls[-1]["active_agent_sessions"] == []
    assert calls[-1]["activity_status_version"] == 1

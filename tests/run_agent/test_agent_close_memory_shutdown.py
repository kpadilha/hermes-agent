from types import SimpleNamespace


def test_agent_close_shuts_down_memory_providers_before_client_close(monkeypatch):
    from run_agent import AIAgent

    events = []

    agent = object.__new__(AIAgent)
    agent.session_id = "sess-close-test"
    agent._active_children_lock = SimpleNamespace(__enter__=lambda self: self, __exit__=lambda self, exc_type, exc, tb: False)
    agent._active_children = []
    agent._memory_manager = SimpleNamespace(
        shutdown_all=lambda: events.append("memory_shutdown")
    )
    agent.client = SimpleNamespace(close=lambda: events.append("client_close"))
    agent._close_openai_client = lambda client, reason, shared: events.append("client_close_wrapper")

    monkeypatch.setattr("tools.process_registry.process_registry.kill_all", lambda task_id=None: events.append("kill_all"))
    monkeypatch.setattr("tools.terminal_tool.cleanup_vm", lambda task_id=None: events.append("cleanup_vm"))
    monkeypatch.setattr("tools.browser_tool.cleanup_browser", lambda task_id=None: events.append("cleanup_browser"))

    agent.close()

    assert "memory_shutdown" in events
    assert "client_close_wrapper" in events
    assert events.index("memory_shutdown") < events.index("client_close_wrapper")
    assert agent.client is None
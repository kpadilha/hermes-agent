import importlib.util
from pathlib import Path


APPLY = Path("/home/krishna/.hermes/scripts/hermes_apply_alignment_candidate.py")


def load_apply():
    spec = importlib.util.spec_from_file_location("alignment_apply", APPLY)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_maybe_restart_gateway_preserves_safe_defer_when_health_probe_fails(monkeypatch):
    mod = load_apply()

    class FakeAutoApply:
        @staticmethod
        def schedule_gateway_restart():
            return {
                "restart_pending": True,
                "restart_deferred": True,
                "reason": "health_probe_failed: RuntimeError: boom",
                "rc": 0,
            }

    class FakeLoader:
        def exec_module(self, module):
            module.schedule_gateway_restart = FakeAutoApply.schedule_gateway_restart

    class FakeSpec:
        loader = FakeLoader()

    monkeypatch.setattr(mod.importlib.util, "spec_from_file_location", lambda *args, **kwargs: FakeSpec())
    monkeypatch.setattr(mod.importlib.util, "module_from_spec", lambda spec: type("Module", (), {})())
    monkeypatch.setattr(mod, "compact_gateway_health", lambda: (_ for _ in ()).throw(RuntimeError("health down")))

    result = mod.maybe_restart_gateway()

    assert result["restart_deferred"] is True
    assert result["rc"] == 0
    assert result["health"] == {"error": "RuntimeError: health down"}

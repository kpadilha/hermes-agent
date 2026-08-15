"""Behavior contract for cron policy-denied toolsets.

``_resolve_cron_disabled_toolsets`` keeps interactive toolsets unavailable to
cron agents and denies ``cronjob`` unless ``cron.allow_agent_scheduling`` is
set. Built-in memory remains denied by default, but a persisted job-scoped
``enabled_toolsets: [memory]`` opt-in removes that policy denial; an explicit
user-level ``agent.disabled_toolsets: [memory]`` still wins.
"""

import pytest

from cron.scheduler import _resolve_cron_disabled_toolsets


ALWAYS_DISABLED = ["messaging", "clarify"]


class TestGateOffDefault:
    def test_empty_config_denies_cronjob(self):
        assert _resolve_cron_disabled_toolsets({}) == [
            "cronjob", "messaging", "clarify", "memory",
        ]

    def test_none_config_denies_cronjob(self):
        assert _resolve_cron_disabled_toolsets(None) == [
            "cronjob", "messaging", "clarify", "memory",
        ]

    def test_cron_section_present_but_gate_absent(self):
        cfg = {"cron": {"preflight": True}}
        assert _resolve_cron_disabled_toolsets(cfg) == [
            "cronjob", "messaging", "clarify", "memory",
        ]

    def test_explicit_false_matches_default(self):
        cfg = {"cron": {"allow_agent_scheduling": False}}
        assert _resolve_cron_disabled_toolsets(cfg) == \
            _resolve_cron_disabled_toolsets({})

    @pytest.mark.parametrize("falsy", [False, None, "", 0])
    def test_falsy_values_keep_gate_off(self, falsy):
        cfg = {"cron": {"allow_agent_scheduling": falsy}}
        disabled = _resolve_cron_disabled_toolsets(cfg)
        assert "cronjob" in disabled


class TestGateOn:
    def test_cronjob_dropped_from_denylist(self):
        cfg = {"cron": {"allow_agent_scheduling": True}}
        disabled = _resolve_cron_disabled_toolsets(cfg)
        assert "cronjob" not in disabled

    def test_interactivity_denials_survive_the_gate(self):
        cfg = {"cron": {"allow_agent_scheduling": True}}
        disabled = _resolve_cron_disabled_toolsets(cfg)
        for name in ALWAYS_DISABLED:
            assert name in disabled

    def test_job_scoped_memory_opt_in_removes_only_cron_policy_denial(self):
        job = {"enabled_toolsets": ["file", "memory"]}
        disabled = _resolve_cron_disabled_toolsets({}, job)
        assert "memory" not in disabled
        assert "messaging" in disabled
        assert "clarify" in disabled

    def test_user_memory_denylist_wins_over_job_opt_in(self):
        cfg = {"agent": {"disabled_toolsets": ["memory"]}}
        job = {"enabled_toolsets": ["memory"]}
        assert "memory" in _resolve_cron_disabled_toolsets(cfg, job)

    def test_user_denylist_wins_over_gate(self):
        # A user who denies cronjob in agent.disabled_toolsets keeps it
        # denied even with the gate on — the gate only removes the built-in
        # policy denial, never the user's own config denylist.
        cfg = {
            "cron": {"allow_agent_scheduling": True},
            "agent": {"disabled_toolsets": ["cronjob"]},
        }
        assert "cronjob" in _resolve_cron_disabled_toolsets(cfg)

    def test_unrelated_user_denylist_layers_without_reviving_cronjob(self):
        cfg = {
            "cron": {"allow_agent_scheduling": True},
            "agent": {"disabled_toolsets": ["browser"]},
        }
        disabled = _resolve_cron_disabled_toolsets(cfg)
        assert "browser" in disabled
        assert "cronjob" not in disabled


class TestUserLayerUnchanged:
    def test_user_denylist_still_layers_when_gate_off(self):
        cfg = {"agent": {"disabled_toolsets": ["browser", "cronjob"]}}
        disabled = _resolve_cron_disabled_toolsets(cfg)
        assert "browser" in disabled
        # No duplicate when the user names an already-denied toolset.
        assert disabled.count("cronjob") == 1

    def test_blank_and_whitespace_entries_ignored(self):
        cfg = {
            "cron": {"allow_agent_scheduling": True},
            "agent": {"disabled_toolsets": ["", "  ", "browser"]},
        }
        disabled = _resolve_cron_disabled_toolsets(cfg)
        assert "browser" in disabled
        assert "" not in disabled

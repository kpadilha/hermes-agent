"""Tests for hermes_cli.gateway."""

import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch, call

import pytest

import hermes_cli.gateway as gateway


def _install_fake_gateway_run(monkeypatch, start_gateway):
    module = ModuleType("gateway.run")
    module.start_gateway = start_gateway
    monkeypatch.setitem(sys.modules, "gateway.run", module)


def test_run_gateway_exits_cleanly_on_keyboard_interrupt(monkeypatch, capsys):
    calls = []

    def fake_start_gateway(*, replace, verbosity):
        calls.append((replace, verbosity))
        return object()

    def fake_asyncio_run(coro):
        raise KeyboardInterrupt

    _install_fake_gateway_run(monkeypatch, fake_start_gateway)
    monkeypatch.setattr(gateway.asyncio, "run", fake_asyncio_run)

    gateway.run_gateway()

    out = capsys.readouterr().out
    assert calls == [(False, 0)]
    assert "Press Ctrl+C to stop" in out
    assert "Gateway stopped." in out


def test_run_gateway_exits_nonzero_when_start_gateway_reports_failure(monkeypatch):
    calls = []

    def fake_start_gateway(*, replace, verbosity):
        calls.append((replace, verbosity))
        return object()

    _install_fake_gateway_run(monkeypatch, fake_start_gateway)
    monkeypatch.setattr(gateway.asyncio, "run", lambda coro: False)

    with pytest.raises(SystemExit) as exc_info:
        gateway.run_gateway(verbose=1, quiet=True, replace=True)

    assert exc_info.value.code == 1
    assert calls == [(True, None)]


def test_build_architecture_dashboard_summarizes_known_and_missing_layers():
    dashboard = gateway._build_architecture_dashboard(
        runtime_state={
            "lcm_runtime": {
                "scorecard": {
                    "runtime_health": "ok",
                    "workflows": {
                        "fallback_activation": {"success": 1, "failure": 0, "total": 1, "success_rate_pct": 100.0},
                    },
                }
            }
        },
        lcm_gateway={
            "scorecard": {
                "runtime_health": "degraded",
                "workflows": {
                    "health_check_execution": {"success": 0, "failure": 1, "total": 1, "success_rate_pct": 0.0},
                },
            }
        },
    )

    assert dashboard["hermes_acts"]["status"] == "ok"
    assert dashboard["honcho_remembers"]["status"] == "unknown"
    assert dashboard["lcm_proves"]["status"] == "degraded"
    assert dashboard["overall"]["status"] == "degraded"


def test_build_architecture_dashboard_integrates_memory_and_recent_turn_scorecards():
    dashboard = gateway._build_architecture_dashboard(
        runtime_state={
            "lcm_runtime": {
                "scorecard": {
                    "runtime_health": "ok",
                    "workflows": {
                        "fallback_activation": {"success": 1, "failure": 0, "total": 1, "success_rate_pct": 100.0},
                    },
                }
            },
            "lcm_memory": {
                "scorecard": {
                    "memory_sync_health": "ok",
                    "workflows": {
                        "memory_write_propagation": {"success": 2, "failure": 0, "total": 2, "success_rate_pct": 100.0},
                    },
                }
            },
            "lcm_recent_turn": {
                "scorecard": {
                    "continuity_health": "ok",
                    "workflows": {
                        "short_confirmation_binding": {"success": 1, "failure": 0, "total": 1, "success_rate_pct": 100.0},
                    },
                }
            },
        },
        lcm_gateway={
            "scorecard": {
                "runtime_health": "ok",
                "workflows": {
                    "health_check_execution": {"success": 1, "failure": 0, "total": 1, "success_rate_pct": 100.0},
                },
            }
        },
    )

    assert dashboard["hermes_acts"]["status"] == "ok"
    assert dashboard["honcho_remembers"]["status"] == "ok"
    assert dashboard["lcm_proves"]["status"] == "ok"
    assert dashboard["honcho_remembers"]["signals"]["memory_write_propagation"]["success"] == 2
    assert dashboard["hermes_acts"]["signals"]["short_confirmation_binding"]["success"] == 1
    assert dashboard["overall"]["status"] == "ok"


def test_build_architecture_dashboard_marks_lcm_proves_unknown_when_required_proof_is_missing():
    dashboard = gateway._build_architecture_dashboard(
        runtime_state={
            "active_agents": 1,
            "lcm_runtime": {
                "scorecard": {
                    "runtime_health": "ok",
                    "workflows": {
                        "fallback_activation": {"success": 1, "failure": 0, "total": 1, "success_rate_pct": 100.0},
                    },
                }
            },
            "lcm_memory": {
                "scorecard": {
                    "memory_sync_health": "ok",
                    "workflows": {
                        "memory_write_propagation": {"success": 2, "failure": 0, "total": 2, "success_rate_pct": 100.0},
                    },
                }
            },
        },
        lcm_gateway={
            "scorecard": {
                "runtime_health": "ok",
                "workflows": {
                    "health_check_execution": {"success": 1, "failure": 0, "total": 1, "success_rate_pct": 100.0},
                },
            }
        },
    )

    assert dashboard["hermes_acts"]["status"] == "ok"
    assert dashboard["honcho_remembers"]["status"] == "ok"
    assert dashboard["lcm_proves"]["status"] == "unknown"
    assert dashboard["overall"]["status"] == "unknown"


def test_build_architecture_dashboard_ignores_runtime_health_without_matching_workflow():
    dashboard = gateway._build_architecture_dashboard(
        runtime_state={
            "lcm_runtime": {
                "scorecard": {
                    "runtime_health": "ok",
                    "workflows": {
                        "health_check_execution": {"success": 1, "failure": 0, "total": 1, "success_rate_pct": 100.0},
                    },
                }
            },
            "lcm_memory": {
                "scorecard": {
                    "memory_sync_health": "ok",
                    "workflows": {
                        "memory_audit": {"success": 1, "failure": 0, "total": 1, "success_rate_pct": 100.0},
                    },
                }
            },
        },
        lcm_gateway={
            "scorecard": {
                "runtime_health": "ok",
                "workflows": {
                    "health_check_execution": {"success": 1, "failure": 0, "total": 1, "success_rate_pct": 100.0},
                },
            }
        },
    )

    assert dashboard["hermes_acts"]["status"] == "unknown"
    assert dashboard["lcm_proves"]["status"] == "unknown"


def test_gateway_health_json_uses_runtime_and_api_probe(monkeypatch, capsys):
    monkeypatch.setattr(
        gateway,
        "build_gateway_health_payload",
        lambda system=False: {
            "manager": "systemd",
            "service_installed": True,
            "service_running": True,
            "gateway_pids": [123],
            "running": True,
            "runtime_state": {"gateway_state": "running"},
            "api_server": {
                "configured": True,
                "host": "127.0.0.1",
                "port": 8642,
                "health": {"ok": True},
                "health_detailed": {"ok": True},
            },
            "lcm_gateway": {
                "scorecard": {"runtime_health": "ok"},
            },
        },
    )

    gateway.gateway_command(SimpleNamespace(gateway_command="health", system=False, json=True))

    payload = json.loads(capsys.readouterr().out)
    assert payload["runtime_state"]["gateway_state"] == "running"
    assert payload["api_server"]["configured"] is True
    assert payload["lcm_gateway"]["scorecard"]["runtime_health"] == "ok"


def test_gateway_health_text_reports_api_server_status(monkeypatch, capsys):
    monkeypatch.setattr(
        gateway,
        "build_gateway_health_payload",
        lambda system=False: {
            "manager": "systemd",
            "service_installed": True,
            "service_running": True,
            "gateway_pids": [123],
            "running": True,
            "runtime_state": {
                "gateway_state": "running",
                "platforms": {
                    "telegram": {"state": "connected"},
                    "api_server": {"state": "connected"},
                },
            },
            "api_server": {
                "configured": True,
                "host": "127.0.0.1",
                "port": 8642,
                "health": {"ok": True},
                "health_detailed": {"ok": True},
            },
            "lcm_gateway": {
                "scorecard": {"runtime_health": "ok"},
            },
            "architecture_dashboard": {
                "hermes_acts": {"status": "ok"},
                "honcho_remembers": {"status": "unknown"},
                "lcm_proves": {"status": "ok"},
                "overall": {"status": "ok"},
            },
        },
    )

    gateway.gateway_command(SimpleNamespace(gateway_command="health", system=False, json=False))

    out = capsys.readouterr().out
    assert "Gateway runtime: running" in out
    assert "API server configured: http://127.0.0.1:8642" in out
    assert "/health: ok" in out
    assert "/health/detailed: ok" in out
    assert "Gateway proof health: ok" in out
    assert "Architecture dashboard:" in out


def test_gateway_health_json_exposes_architecture_dashboard(monkeypatch, capsys):
    monkeypatch.setattr(
        gateway,
        "build_gateway_health_payload",
        lambda system=False: {
            "runtime_state": {
                "gateway_state": "running",
                "lcm_runtime": {
                    "scorecard": {
                        "runtime_health": "ok",
                        "workflows": {
                            "fallback_activation": {"success": 1, "failure": 0, "total": 1, "success_rate_pct": 100.0}
                        },
                    }
                },
            },
            "api_server": {
                "configured": True,
                "host": "127.0.0.1",
                "port": 8642,
                "health": {"ok": True},
                "health_detailed": {"ok": True},
            },
            "lcm_gateway": {
                "scorecard": {
                    "runtime_health": "ok",
                    "workflows": {
                        "health_check_execution": {"success": 1, "failure": 0, "total": 1, "success_rate_pct": 100.0}
                    },
                }
            },
            "architecture_dashboard": {
                "hermes_acts": {"status": "ok"},
                "honcho_remembers": {"status": "unknown"},
                "lcm_proves": {"status": "ok"},
                "overall": {"status": "ok"},
            },
        },
    )

    gateway.gateway_command(SimpleNamespace(gateway_command="health", system=False, json=True))

    payload = json.loads(capsys.readouterr().out)
    assert payload["architecture_dashboard"]["hermes_acts"]["status"] == "ok"
    assert payload["architecture_dashboard"]["honcho_remembers"]["status"] == "unknown"
    assert payload["architecture_dashboard"]["lcm_proves"]["status"] == "ok"


def test_build_gateway_health_payload_reads_recent_turn_and_memory_from_runtime_status(monkeypatch):
    runtime_state = {
        "gateway_state": "running",
        "lcm_runtime": {
            "scorecard": {
                "runtime_health": "ok",
                "workflows": {
                    "fallback_activation": {"success": 1, "failure": 0, "total": 1, "success_rate_pct": 100.0},
                },
            },
        },
        "lcm_recent_turn": {
            "scorecard": {
                "continuity_health": "ok",
                "workflows": {
                    "short_confirmation_binding": {"success": 2, "failure": 0, "total": 2, "success_rate_pct": 100.0},
                },
            },
        },
        "lcm_memory": {
            "scorecard": {
                "memory_sync_health": "ok",
                "workflows": {
                    "memory_write_propagation": {"success": 3, "failure": 0, "total": 3, "success_rate_pct": 100.0},
                },
            },
        },
    }
    monkeypatch.setattr(gateway, "get_gateway_runtime_snapshot", lambda system=False: SimpleNamespace(
        manager="systemd",
        service_installed=True,
        service_running=True,
        gateway_pids=[123],
        running=True,
    ))
    monkeypatch.setattr(gateway, "_load_runtime_health_state", lambda: runtime_state)
    monkeypatch.setattr(gateway, "_probe_api_server_health", lambda url, timeout=2.0: {"ok": True, "url": url})
    monkeypatch.setattr(gateway, "_record_gateway_workflow_event", lambda state, workflow, outcome, **kwargs: {
        **state,
        "lcm_gateway": {
            "scorecard": {
                "runtime_health": "ok",
                "workflows": {
                    "health_check_execution": {"success": 1, "failure": 0, "total": 1, "success_rate_pct": 100.0},
                },
            },
        },
    })
    from gateway.config import Platform

    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: SimpleNamespace(platforms={
        Platform.API_SERVER: SimpleNamespace(enabled=True, extra={"host": "127.0.0.1", "port": 8642})
    }))
    write_calls = []
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kwargs: write_calls.append(kwargs))

    payload = gateway.build_gateway_health_payload(system=False)

    expected_lcm_gateway = {
        "scorecard": {
            "runtime_health": "ok",
            "workflows": {
                "health_check_execution": {"success": 1, "failure": 0, "total": 1, "success_rate_pct": 100.0},
            },
        },
    }

    assert payload["runtime_state"]["lcm_recent_turn"]["scorecard"]["continuity_health"] == "ok"
    assert payload["runtime_state"]["lcm_memory"]["scorecard"]["memory_sync_health"] == "ok"
    assert payload["architecture_dashboard"]["hermes_acts"]["status"] == "ok"
    assert payload["architecture_dashboard"]["honcho_remembers"]["status"] == "ok"
    assert payload["architecture_dashboard"]["lcm_proves"]["status"] == "ok"
    assert payload["lcm_gateway"] == expected_lcm_gateway
    assert write_calls == [{"lcm_gateway": expected_lcm_gateway}]


class TestSystemdLingerStatus:
    def test_reports_enabled(self, monkeypatch):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setenv("USER", "alice")
        monkeypatch.setattr(
            gateway.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="yes\n", stderr=""),
        )
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/loginctl")

        assert gateway.get_systemd_linger_status() == (True, "")

    def test_reports_disabled(self, monkeypatch):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setenv("USER", "alice")
        monkeypatch.setattr(
            gateway.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="no\n", stderr=""),
        )
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/loginctl")

        assert gateway.get_systemd_linger_status() == (False, "")

    def test_reports_termux_as_not_supported(self, monkeypatch):
        monkeypatch.setattr(gateway, "is_termux", lambda: True)

        assert gateway.get_systemd_linger_status() == (None, "not supported in Termux")


class TestContainerSystemdSupport:
    def test_supports_systemd_services_in_container_with_user_manager(self, monkeypatch):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr(gateway, "is_wsl", lambda: False)
        monkeypatch.setattr(gateway, "is_container", lambda: True)
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemctl")
        monkeypatch.setattr(gateway, "_systemd_operational", lambda system=False: not system)

        assert gateway.supports_systemd_services() is True

    def test_supports_systemd_services_in_container_with_system_manager(self, monkeypatch):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr(gateway, "is_wsl", lambda: False)
        monkeypatch.setattr(gateway, "is_container", lambda: True)
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemctl")
        monkeypatch.setattr(gateway, "_systemd_operational", lambda system=False: system)

        assert gateway.supports_systemd_services() is True

    def test_supports_systemd_services_in_container_without_systemd(self, monkeypatch):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr(gateway, "is_wsl", lambda: False)
        monkeypatch.setattr(gateway, "is_container", lambda: True)
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemctl")
        monkeypatch.setattr(gateway, "_systemd_operational", lambda system=False: False)

        assert gateway.supports_systemd_services() is False


def test_gateway_install_in_container_with_operational_systemd_uses_systemd(monkeypatch):
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: True)
    monkeypatch.setattr(gateway, "is_wsl", lambda: False)
    monkeypatch.setattr(gateway, "is_macos", lambda: False)
    monkeypatch.setattr(gateway, "is_managed", lambda: False)

    calls = []
    monkeypatch.setattr(
        gateway,
        "systemd_install",
        lambda force=False, system=False, run_as_user=None: calls.append((force, system, run_as_user)),
    )

    args = SimpleNamespace(
        gateway_command="install",
        force=False,
        system=False,
        run_as_user=None,
    )
    gateway.gateway_command(args)

    assert calls == [(False, False, None)]


def test_gateway_start_in_container_with_operational_systemd_uses_systemd(monkeypatch):
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: True)
    monkeypatch.setattr(gateway, "is_wsl", lambda: False)
    monkeypatch.setattr(gateway, "is_macos", lambda: False)

    calls = []
    monkeypatch.setattr(gateway, "systemd_start", lambda system=False: calls.append(system))

    args = SimpleNamespace(gateway_command="start", system=False, all=False)
    gateway.gateway_command(args)

    assert calls == [False]


def test_systemd_status_warns_when_linger_disabled(monkeypatch, tmp_path, capsys):
    unit_path = tmp_path / "hermes-gateway.service"
    unit_path.write_text("[Unit]\n")

    monkeypatch.setattr(gateway, "get_systemd_unit_path", lambda system=False: unit_path)
    monkeypatch.setattr(gateway, "get_systemd_linger_status", lambda: (False, ""))

    def fake_run(cmd, capture_output=False, text=False, check=False, **kwargs):
        if cmd[:4] == ["systemctl", "--user", "status", gateway.get_service_name()]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["systemctl", "--user", "is-active"]:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        if cmd[:3] == ["systemctl", "--user", "show"]:
            return SimpleNamespace(
                returncode=0,
                stdout="ActiveState=active\nSubState=running\nResult=success\nExecMainStatus=0\n",
                stderr="",
            )
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(gateway.subprocess, "run", fake_run)

    gateway.systemd_status(deep=False)

    out = capsys.readouterr().out
    assert "gateway service is running" in out
    assert "Systemd linger is disabled" in out
    assert "loginctl enable-linger" in out


def test_systemd_install_checks_linger_status(monkeypatch, tmp_path, capsys):
    unit_path = tmp_path / "systemd" / "user" / "hermes-gateway.service"

    monkeypatch.setattr(gateway, "get_systemd_unit_path", lambda system=False: unit_path)

    calls = []
    helper_calls = []

    def fake_run(cmd, check=False, **kwargs):
        calls.append((cmd, check))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gateway.subprocess, "run", fake_run)
    monkeypatch.setattr(gateway, "_ensure_linger_enabled", lambda: helper_calls.append(True))

    gateway.systemd_install(force=False)

    out = capsys.readouterr().out
    assert unit_path.exists()
    assert [cmd for cmd, _ in calls] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", gateway.get_service_name()],
    ]
    assert helper_calls == [True]
    assert "User service installed and enabled" in out


def test_systemd_install_system_scope_skips_linger_and_uses_systemctl(monkeypatch, tmp_path, capsys):
    unit_path = tmp_path / "etc" / "systemd" / "system" / "hermes-gateway.service"

    monkeypatch.setattr(gateway, "get_systemd_unit_path", lambda system=False: unit_path)
    monkeypatch.setattr(
        gateway,
        "generate_systemd_unit",
        lambda system=False, run_as_user=None: f"scope={system} user={run_as_user}\n",
    )
    monkeypatch.setattr(gateway, "_require_root_for_system_service", lambda action: None)

    calls = []
    helper_calls = []

    def fake_run(cmd, check=False, **kwargs):
        calls.append((cmd, check))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gateway.subprocess, "run", fake_run)
    monkeypatch.setattr(gateway, "_ensure_linger_enabled", lambda: helper_calls.append(True))

    gateway.systemd_install(force=False, system=True, run_as_user="alice")

    out = capsys.readouterr().out
    assert unit_path.exists()
    assert unit_path.read_text(encoding="utf-8") == "scope=True user=alice\n"
    assert [cmd for cmd, _ in calls] == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", gateway.get_service_name()],
    ]
    assert helper_calls == []
    assert "Configured to run as: alice" not in out  # generated test unit has no User= line
    assert "System service installed and enabled" in out


def test_conflicting_systemd_units_warning(monkeypatch, tmp_path, capsys):
    user_unit = tmp_path / "user" / "hermes-gateway.service"
    system_unit = tmp_path / "system" / "hermes-gateway.service"
    user_unit.parent.mkdir(parents=True)
    system_unit.parent.mkdir(parents=True)
    user_unit.write_text("[Unit]\n", encoding="utf-8")
    system_unit.write_text("[Unit]\n", encoding="utf-8")

    monkeypatch.setattr(
        gateway,
        "get_systemd_unit_path",
        lambda system=False: system_unit if system else user_unit,
    )

    gateway.print_systemd_scope_conflict_warning()

    out = capsys.readouterr().out
    assert "Both user and system gateway services are installed" in out
    assert "hermes gateway uninstall" in out
    assert "--system" in out


def test_install_linux_gateway_from_setup_system_choice_without_root_prints_followup(monkeypatch, capsys):
    monkeypatch.setattr(gateway, "prompt_linux_gateway_install_scope", lambda: "system")
    monkeypatch.setattr(gateway.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(gateway, "_default_system_service_user", lambda: "alice")
    monkeypatch.setattr(gateway, "systemd_install", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not install")))

    scope, did_install = gateway.install_linux_gateway_from_setup(force=False)

    out = capsys.readouterr().out
    assert (scope, did_install) == ("system", False)
    assert "sudo hermes gateway install --system --run-as-user alice" in out
    assert "sudo hermes gateway start --system" in out


def test_install_linux_gateway_from_setup_system_choice_as_root_installs(monkeypatch):
    monkeypatch.setattr(gateway, "prompt_linux_gateway_install_scope", lambda: "system")
    monkeypatch.setattr(gateway.os, "geteuid", lambda: 0)
    monkeypatch.setattr(gateway, "_default_system_service_user", lambda: "alice")

    calls = []
    monkeypatch.setattr(
        gateway,
        "systemd_install",
        lambda force=False, system=False, run_as_user=None: calls.append((force, system, run_as_user)),
    )

    scope, did_install = gateway.install_linux_gateway_from_setup(force=True)

    assert (scope, did_install) == ("system", True)
    assert calls == [(True, True, "alice")]


def test_find_gateway_pids_falls_back_to_pid_file_when_process_scan_fails(monkeypatch):
    monkeypatch.setattr(gateway, "_get_service_pids", lambda: set())
    monkeypatch.setattr(gateway, "is_windows", lambda: False)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 321)

    def fake_run(cmd, **kwargs):
        if cmd[:4] == ["ps", "-A", "eww", "-o"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="ps failed")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(gateway.subprocess, "run", fake_run)

    assert gateway.find_gateway_pids() == [321]


# ---------------------------------------------------------------------------
# _wait_for_gateway_exit
# ---------------------------------------------------------------------------


class TestWaitForGatewayExit:
    """PID-based wait with force-kill on timeout."""

    def test_returns_immediately_when_no_pid(self, monkeypatch):
        """If get_running_pid returns None, exit instantly."""
        monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
        # Should return without sleeping at all.
        gateway._wait_for_gateway_exit(timeout=1.0, force_after=0.5)

    def test_returns_when_process_exits_gracefully(self, monkeypatch):
        """Process exits after a couple of polls — no SIGKILL needed."""
        poll_count = 0

        def mock_get_running_pid():
            nonlocal poll_count
            poll_count += 1
            return 12345 if poll_count <= 2 else None

        monkeypatch.setattr("gateway.status.get_running_pid", mock_get_running_pid)
        monkeypatch.setattr("time.sleep", lambda _: None)

        gateway._wait_for_gateway_exit(timeout=10.0, force_after=999.0)
        # Should have polled until None was returned.
        assert poll_count == 3

    def test_force_kills_after_grace_period(self, monkeypatch):
        """When the process doesn't exit, force-kill the saved PID."""

        # Simulate monotonic time advancing past force_after
        call_num = 0
        def fake_monotonic():
            nonlocal call_num
            call_num += 1
            # First two calls: initial deadline + force_deadline setup (time 0)
            # Then each loop iteration advances time
            return call_num * 2.0  # 2, 4, 6, 8, ...

        kills = []
        def mock_terminate(pid, force=False):
            kills.append((pid, force))

        # get_running_pid returns the PID until kill is sent, then None
        def mock_get_running_pid():
            return None if kills else 42

        monkeypatch.setattr("time.monotonic", fake_monotonic)
        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr("gateway.status.get_running_pid", mock_get_running_pid)
        monkeypatch.setattr(gateway, "terminate_pid", mock_terminate)

        gateway._wait_for_gateway_exit(timeout=10.0, force_after=5.0)
        assert (42, True) in kills

    def test_handles_process_already_gone_on_kill(self, monkeypatch):
        """ProcessLookupError during force-kill is not fatal."""

        call_num = 0
        def fake_monotonic():
            nonlocal call_num
            call_num += 1
            return call_num * 3.0  # Jump past force_after quickly

        def mock_terminate(pid, force=False):
            raise ProcessLookupError

        monkeypatch.setattr("time.monotonic", fake_monotonic)
        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr("gateway.status.get_running_pid", lambda: 99)
        monkeypatch.setattr(gateway, "terminate_pid", mock_terminate)

        # Should not raise — ProcessLookupError means it's already gone.
        gateway._wait_for_gateway_exit(timeout=10.0, force_after=2.0)

    def test_kill_gateway_processes_force_uses_helper(self, monkeypatch):
        calls = []

        monkeypatch.setattr(gateway, "find_gateway_pids", lambda exclude_pids=None, all_profiles=False: [11, 22])
        monkeypatch.setattr(gateway, "terminate_pid", lambda pid, force=False: calls.append((pid, force)))

        killed = gateway.kill_gateway_processes(force=True)

        assert killed == 2
        assert calls == [(11, True), (22, True)]


class TestStopProfileGateway:
    def test_stop_profile_gateway_keeps_pid_file_when_process_still_running(self, monkeypatch):
        calls = {"kill": 0, "remove": 0}

        monkeypatch.setattr("gateway.status.get_running_pid", lambda: 12345)
        monkeypatch.setattr(
            gateway.os,
            "kill",
            lambda pid, sig: calls.__setitem__("kill", calls["kill"] + 1),
        )
        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(
            "gateway.status.remove_pid_file",
            lambda: calls.__setitem__("remove", calls["remove"] + 1),
        )

        assert gateway.stop_profile_gateway() is True
        assert calls["kill"] == 21
        assert calls["remove"] == 0

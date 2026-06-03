"""Tests for hermes_cli.gateway."""

import argparse
import json
import os
import signal
import subprocess
import sys
import textwrap
from types import ModuleType, SimpleNamespace

import pytest

import hermes_cli.gateway as gateway


def _install_fake_gateway_run(monkeypatch, start_gateway):
    module = ModuleType("gateway.run")
    module.start_gateway = start_gateway

    def _exit_after_graceful_shutdown(code):
        if code:
            raise SystemExit(code)

    setattr(module, "_exit_after_graceful_shutdown", _exit_after_graceful_shutdown)
    monkeypatch.setitem(sys.modules, "gateway.run", module)
    # ``run_gateway()`` calls ``refresh_systemd_unit_if_needed()`` on every
    # invocation so that restart settings stay current after exit-code-75
    # respawns. That helper writes to ``Path.home() / ".config/systemd/user
    # /hermes-gateway.service"`` and runs ``systemctl --user daemon-reload``
    # — both target the *real* user environment because the conftest only
    # sandboxes ``HERMES_HOME``, not ``HOME``. Tests that drive
    # ``run_gateway()`` end-to-end with a fake ``start_gateway`` MUST stub
    # the refresh call too, or every run rewrites the developer's installed
    # unit (baking in the test's pytest-tmp ``HERMES_HOME`` value, which
    # systemd then uses on the next boot — silently breaking the gateway
    # for the developer).
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(
        gateway, "refresh_systemd_unit_if_needed", lambda system=False: False
    )
    # Neutralize the supervised-gateway conflict guard by default so these
    # end-to-end tests don't trip over a launchd/systemd gateway that happens
    # to be installed+running on the developer's machine. Conflict-guard tests
    # override this snapshot after calling the helper.
    monkeypatch.setattr(
        gateway,
        "get_gateway_runtime_snapshot",
        lambda *a, **k: gateway.GatewayRuntimeSnapshot(manager="manual process"),
    )




@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY coverage")
@pytest.mark.parametrize(
    ("stdin_is_tty", "outcome", "expected_exit"),
    [
        (True, "systemexit:75", 75),
        (False, "systemexit:75", 75),
        (False, "systemexit:78", 78),
        (False, "failure", 1),
    ],
)
def test_gateway_run_subprocess_preserves_daemon_exit_codes(
    tmp_path, stdin_is_tty, outcome, expected_exit
):
    """TTY state must not rewrite the gateway's process-level exit contract.

    Exit 75 is the intentional systemd/launchd restart handoff, exit 78 is a
    fatal configuration error, and a false startup result is a generic failure.
    In particular, a non-TTY daemon launch must not blanket-catch SystemExit,
    because doing so would hide genuine startup/configuration failures.
    """
    script = textwrap.dedent(
        """
        import os
        import sys
        import types

        import hermes_cli.gateway as gateway_cli

        outcome = os.environ["HERMES_TEST_GATEWAY_OUTCOME"]

        async def start_gateway(*, replace, verbosity):
            if outcome == "failure":
                return False
            raise SystemExit(int(outcome.split(":", 1)[1]))

        fake_run = types.ModuleType("gateway.run")
        fake_run.start_gateway = start_gateway
        setattr(fake_run, "_exit_after_graceful_shutdown", sys.exit)
        sys.modules["gateway.run"] = fake_run

        gateway_cli._guard_official_docker_root_gateway = lambda: None
        gateway_cli._guard_named_profile_under_multiplexer = lambda force=False: None
        gateway_cli._guard_supervised_gateway_conflict = lambda force=False: None
        gateway_cli._guard_existing_gateway_process_conflict = lambda replace=False: None
        gateway_cli.supports_systemd_services = lambda: False
        gateway_cli.run_gateway()
        """
    )
    env = {
        **os.environ,
        "HERMES_HOME": str(tmp_path),
        "HERMES_GATEWAY_EXIT_DIAG": "0",
        "HERMES_TEST_GATEWAY_OUTCOME": outcome,
        "INVOCATION_ID": "systemd-test",
    }

    master_fd = slave_fd = None
    try:
        if stdin_is_tty:
            # Imported here, not at module scope: ``pty`` pulls in ``termios``,
            # which does not exist on Windows, so a top-level import raises
            # ModuleNotFoundError during *collection* — before the skipif above
            # can take effect — and takes the whole module's Windows-viable
            # tests down with it.
            import pty

            master_fd, slave_fd = pty.openpty()
            stdin = slave_fd
        else:
            stdin = subprocess.DEVNULL
        completed = subprocess.run(
            [sys.executable, "-c", script],
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
    finally:
        if slave_fd is not None:
            os.close(slave_fd)
        if master_fd is not None:
            os.close(master_fd)

    assert completed.returncode == expected_exit, completed.stderr


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


def _clear_supervisor_markers(monkeypatch):
    """Make ``_running_under_gateway_supervisor()`` report a plain shell."""
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("HERMES_S6_SUPERVISED_CHILD", raising=False)
    # Interactive macOS shells inherit XPC_SERVICE_NAME="0"; launchd jobs get
    # the real label. Default to the shell sentinel so the guard can fire.
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")


def _running_snapshot(manager="systemd (user)"):
    return gateway.GatewayRuntimeSnapshot(
        manager=manager, service_installed=True, service_running=True
    )


def test_s6_runtime_snapshot_reports_supervised_service(monkeypatch, tmp_path):
    service_dir = tmp_path / "gateway-default"
    service_dir.mkdir()

    class FakeS6Manager:
        scandir = tmp_path

        def is_running(self, name):
            assert name == "gateway-default"
            return True

    monkeypatch.setattr(gateway, "is_linux", lambda: True)
    monkeypatch.setattr("hermes_constants.is_container", lambda: True)
    monkeypatch.setattr("hermes_cli.service_manager.detect_service_manager", lambda: "s6")
    monkeypatch.setattr("hermes_cli.service_manager.get_service_manager", lambda: FakeS6Manager())
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda: [123])
    monkeypatch.setattr(gateway, "_profile_suffix", lambda: "")

    snapshot = gateway.get_gateway_runtime_snapshot()

    assert snapshot.manager == "s6 (container supervisor)"
    assert snapshot.service_installed is True
    assert snapshot.service_running is True
    assert snapshot.service_scope == "s6"
    assert snapshot.gateway_pids == (123,)






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






@pytest.mark.skipif(
    sys.platform == "win32",
    reason="systemd user-linger is Linux-only (drives os.getuid())",
)
def test_systemd_install_checks_linger_status(monkeypatch, tmp_path, capsys):
    unit_path = tmp_path / "systemd" / "user" / "hermes-gateway.service"

    monkeypatch.setattr(gateway, "get_systemd_unit_path", lambda system=False: unit_path)
    # Synthetic unit with a non-temp home: the real generator bakes the
    # hermetic test HERMES_HOME (a tmp dir), which the temp-home write
    # guard correctly refuses.
    monkeypatch.setattr(
        gateway,
        "generate_systemd_unit",
        lambda system=False, run_as_user=None: (
            '[Service]\nEnvironment="HERMES_HOME=/home/alice/.hermes"\n'
        ),
    )

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










def test_gateway_install_noninteractive_skips_legacy_unit_prompt(monkeypatch, tmp_path):
    """In non-TTY, the legacy-unit removal prompt in systemd_install is skipped.

    Covers the second hidden prompt that --start-now/--start-on-login do not
    guard. Originally contributed via PR #42124 (kyssta-exe).
    """
    monkeypatch.setattr(gateway, "has_legacy_hermes_units", lambda: True)

    calls = []
    monkeypatch.setattr(
        gateway,
        "prompt_yes_no",
        lambda question, default=True: calls.append(("prompt", question)) or True,
    )
    monkeypatch.setattr(gateway, "remove_legacy_hermes_units", lambda interactive=False: calls.append(("remove_legacy",)))
    monkeypatch.setattr(gateway, "print_legacy_unit_warning", lambda: None)

    fake_path = tmp_path / "hermes-gateway.service"
    monkeypatch.setattr(gateway, "get_systemd_unit_path", lambda system=False: fake_path)
    monkeypatch.setattr(gateway, "generate_systemd_unit", lambda system=False, run_as_user=None: "[Service]")
    monkeypatch.setattr(gateway, "_run_systemctl", lambda *a, **kw: None)
    monkeypatch.setattr(gateway, "_ensure_linger_enabled", lambda: None)
    monkeypatch.setattr(gateway, "print_systemd_scope_conflict_warning", lambda: None)
    monkeypatch.setattr(gateway, "_service_scope_label", lambda system=False: "user")

    gateway.systemd_install(non_interactive=True)

    # Legacy units removed without prompting.
    assert ("remove_legacy",) in calls
    assert all(c[0] != "prompt" for c in calls)












# ---------------------------------------------------------------------------
# _wait_for_gateway_exit
# ---------------------------------------------------------------------------


class TestWaitForGatewayExit:
    """PID-based wait with force-kill on timeout."""



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


    def test_kill_gateway_processes_force_uses_helper(self, monkeypatch):
        calls = []

        monkeypatch.setattr(gateway, "find_gateway_pids", lambda exclude_pids=None, all_profiles=False: [11, 22])
        monkeypatch.setattr(gateway, "terminate_pid", lambda pid, force=False: calls.append((pid, force)))

        killed = gateway.kill_gateway_processes(force=True)

        assert killed == 2
        assert calls == [(11, True), (22, True)]


class TestStopProfileGateway:
    def test_stop_profile_gateway_keeps_pid_file_when_process_still_running(self, monkeypatch):
        calls = {"kill": 0, "alive_probes": 0, "remove": 0, "reap_calls": 0}

        monkeypatch.setattr("gateway.status.get_running_pid", lambda: 12345)
        # Post-#21561: the stop loop sends one SIGTERM via ``os.kill`` then
        # polls liveness via ``gateway.status._pid_exists`` (safe on
        # Windows — bpo-14484). Instrument both seams separately.
        monkeypatch.setattr(
            gateway.os,
            "kill",
            lambda pid, sig: calls.__setitem__("kill", calls["kill"] + 1),
        )
        monkeypatch.setattr(
            "gateway.status._pid_exists",
            lambda pid: calls.__setitem__("alive_probes", calls["alive_probes"] + 1) or True,
        )
        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(
            "gateway.status.remove_pid_file",
            lambda: calls.__setitem__("remove", calls["remove"] + 1),
        )
        # Mock the orphan reap so it doesn't scan for real gateway processes
        # (#75936 — stop_profile_gateway now calls _reap_unsupervised_gateway_orphans
        # after killing the pid-file PID).
        monkeypatch.setattr(
            gateway,
            "_reap_unsupervised_gateway_orphans",
            lambda extra_exclude=None: calls.__setitem__("reap_calls", calls["reap_calls"] + 1) or False,
        )

        assert gateway.stop_profile_gateway() is True
        assert calls["kill"] == 1          # one SIGTERM
        assert calls["alive_probes"] == 20 # 20 liveness polls over the 2s window
        assert calls["remove"] == 0
        assert calls["reap_calls"] == 1    # orphan sweep ran after kill

    def test_stop_profile_gateway_excludes_killed_pid_from_orphan_reap(self, monkeypatch):
        """The PID we killed must be excluded from the orphan sweep (#75936)."""
        killed_pid = 99999
        reap_extra_excludes = []

        monkeypatch.setattr("gateway.status.get_running_pid", lambda: killed_pid)
        monkeypatch.setattr(gateway.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)
        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr("gateway.status.remove_pid_file", lambda: None)

        def fake_reap(extra_exclude=None):
            if extra_exclude:
                reap_extra_excludes.append(extra_exclude)
            return False

        monkeypatch.setattr(gateway, "_reap_unsupervised_gateway_orphans", fake_reap)

        assert gateway.stop_profile_gateway() is True
        assert len(reap_extra_excludes) == 1
        assert killed_pid in reap_extra_excludes[0]


def test_module_has_logger():
    """Verify module has a logger instance (regression guard for #27154)."""
    assert hasattr(gateway, "logger")
    assert gateway.logger.name == "hermes_cli.gateway"

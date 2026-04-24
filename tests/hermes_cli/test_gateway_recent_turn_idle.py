import hermes_cli.gateway as gateway


def test_build_architecture_dashboard_does_not_require_recent_turn_proof_when_idle():
    dashboard = gateway._build_architecture_dashboard(
        runtime_state={
            "active_agents": 0,
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
                        "memory_reconcile_projection": {"success": 1, "failure": 0, "total": 1, "success_rate_pct": 100.0},
                    },
                }
            },
        },
        lcm_gateway={
            "scorecard": {
                "runtime_health": "ok",
                "workflows": {
                    "health_check_execution": {"success": 3, "failure": 0, "total": 3, "success_rate_pct": 100.0, "latest_outcome": "success"},
                },
            }
        },
    )

    assert dashboard["hermes_acts"]["status"] == "ok"
    assert dashboard["lcm_proves"]["status"] == "ok"
    assert dashboard["overall"]["status"] == "ok"


def test_build_architecture_dashboard_requires_recent_turn_proof_when_active_agent_exists():
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
                        "memory_reconcile_projection": {"success": 1, "failure": 0, "total": 1, "success_rate_pct": 100.0},
                    },
                }
            },
        },
        lcm_gateway={
            "scorecard": {
                "runtime_health": "ok",
                "workflows": {
                    "health_check_execution": {"success": 3, "failure": 0, "total": 3, "success_rate_pct": 100.0, "latest_outcome": "success"},
                },
            }
        },
    )

    assert dashboard["lcm_proves"]["status"] == "unknown"
    assert dashboard["overall"]["status"] == "unknown"
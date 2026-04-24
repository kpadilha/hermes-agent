from datetime import datetime, timezone

import hermes_cli.gateway as gateway


def test_build_gateway_lcm_scorecard_prefers_latest_success_over_historical_failures():
    scorecard = gateway._build_gateway_lcm_scorecard(
        {
            "workflow_counters": {
                "health_check_execution": {"success": 15, "failure": 2},
            },
            "recent_workflow_events": [
                {"workflow": "health_check_execution", "outcome": "failure", "details": {}},
                {"workflow": "health_check_execution", "outcome": "success", "details": {}},
            ],
        }
    )

    assert scorecard["runtime_health"] == "ok"
    assert scorecard["workflows"]["health_check_execution"]["latest_outcome"] == "success"


def test_build_gateway_lcm_scorecard_marks_degraded_when_latest_event_failed():
    scorecard = gateway._build_gateway_lcm_scorecard(
        {
            "workflow_counters": {
                "health_check_execution": {"success": 15, "failure": 2},
            },
            "recent_workflow_events": [
                {"workflow": "health_check_execution", "outcome": "success", "details": {}},
                {"workflow": "health_check_execution", "outcome": "failure", "details": {}},
            ],
        }
    )

    assert scorecard["runtime_health"] == "degraded"
    assert scorecard["workflows"]["health_check_execution"]["latest_outcome"] == "failure"


def test_build_gateway_lcm_scorecard_marks_health_check_stale_without_fresh_event():
    now = datetime(2026, 4, 24, 21, 45, tzinfo=timezone.utc)
    scorecard = gateway._build_gateway_lcm_scorecard(
        {
            "workflow_counters": {
                "health_check_execution": {"success": 15, "failure": 0},
            },
            "recent_workflow_events": [
                {
                    "workflow": "health_check_execution",
                    "outcome": "success",
                    "details": {},
                    "recorded_at": "2026-04-24T21:30:00+00:00",
                },
            ],
        },
        now=now,
    )

    assert scorecard["runtime_health"] == "unknown"
    assert scorecard["stale_workflows"] == ["health_check_execution"]
    assert scorecard["workflows"]["health_check_execution"]["freshness"] == "stale"


def test_record_gateway_workflow_event_stamps_recorded_at():
    state = gateway._record_gateway_workflow_event({}, "health_check_execution", "success")

    event = state["lcm_gateway"]["recent_workflow_events"][-1]
    assert event["recorded_at"]
    assert gateway._parse_iso_datetime(event["recorded_at"]) is not None

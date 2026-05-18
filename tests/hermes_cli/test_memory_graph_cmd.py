import json
from types import SimpleNamespace

from agent.memory_ledger import BeliefLedger, MemoryWriteGate
from hermes_cli.memory_graph_cmd import build_graph_sync_plan, memory_graph_command


class FakeResult:
    def __init__(self, rows=None, single=None):
        self._rows = rows or []
        self._single = single

    def data(self):
        return self._rows

    def single(self):
        return self._single


class FakeSession:
    def __init__(self):
        self.queries = []
        self.edges = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, query, **params):
        self.queries.append((query, params))
        if "RETURN r.record_id AS record_id" in query:
            return FakeResult([])
        if "RETURN count(r) AS active_preference_edges" in query:
            count = sum(
                1
                for edge in self.edges.values()
                if edge.get("subject") == "Krishna"
                and edge.get("predicate") == "prefers"
                and edge.get("status") == "active"
            )
            return FakeResult(single={"active_preference_edges": count})
        if "MERGE (s)-[r:HERMES_MEMORY_FACT" in query:
            self.edges[params["record_id"]] = dict(params)
        return FakeResult([])


class FakeDriver:
    def __init__(self):
        self.session_obj = FakeSession()
        self.verified = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def verify_connectivity(self):
        self.verified = True

    def session(self, database="neo4j"):
        return self.session_obj


def _seed_ledger(tmp_path):
    ledger = BeliefLedger(tmp_path / "ledger.db")
    gate = MemoryWriteGate(ledger)
    prior = gate.evaluate_and_record(
        target="user",
        content="Krishna prefers SaaS-first memory architecture.",
        source="test",
        evidence_ref="test#superseded",
    )["record"]
    active = gate.evaluate_and_record(
        target="user",
        content="Krishna prefers local-first memory architecture.",
        source="test",
        evidence_ref="test#active",
    )["record"]
    superseded = prior["id"]
    deleted = gate.evaluate_and_record(
        target="memory",
        content="System uses temporary deleted fact.",
        source="test",
        evidence_ref="test#deleted",
    )["record"]
    gate.delete_record(record_id=deleted["id"], source="test", evidence_ref="test#delete")
    return ledger, active["id"], superseded, deleted["id"]


def test_build_graph_sync_plan_preserves_statuses_and_reports_missing_without_writing(tmp_path):
    ledger, active_id, superseded_id, deleted_id = _seed_ledger(tmp_path)

    plan = build_graph_sync_plan(ledger=ledger, existing_edges=[])

    assert plan["success"] is True
    assert plan["mode"] == "dry-run"
    assert plan["summary"]["to_upsert"] == 3
    statuses = {item["record_id"]: item["status"] for item in plan["changes"]}
    assert statuses[active_id] == "active"
    assert statuses[superseded_id] == "superseded"
    assert statuses[deleted_id] == "deleted"
    assert all(item["action"] == "upsert" for item in plan["changes"])
    assert plan["validation"]["active_krishna_preference_edges"] == 1
    assert plan["validation"]["active_krishna_preference_ok"] is True


def test_build_graph_sync_plan_avoids_duplicate_relationships_when_projection_is_current(tmp_path):
    ledger, active_id, _, _ = _seed_ledger(tmp_path)
    record = ledger.get_record(active_id)
    existing = [
        {
            "record_id": active_id,
            "subject": record["subject"],
            "predicate": record["predicate"],
            "object": record["object"][:240],
            "status": record["status"],
            "evidence_ref": record["evidence_ref"],
            "source": record["source"],
        }
    ]

    plan = build_graph_sync_plan(ledger=ledger, existing_edges=existing)

    assert active_id not in {item["record_id"] for item in plan["changes"]}
    assert plan["summary"]["already_current"] == 1


def test_memory_graph_command_dry_run_outputs_json_and_does_not_apply(tmp_path, capsys):
    ledger, *_ = _seed_ledger(tmp_path)
    fake_driver = FakeDriver()

    memory_graph_command(
        SimpleNamespace(graph_command="sync", dry_run=True, apply=False, json=True),
        ledger=ledger,
        driver_factory=lambda: fake_driver,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert payload["summary"]["to_upsert"] == 3
    assert fake_driver.session_obj.edges == {}


def test_memory_graph_command_apply_writes_edges_and_validates_single_active_preference(tmp_path, capsys):
    ledger, *_ = _seed_ledger(tmp_path)
    fake_driver = FakeDriver()

    memory_graph_command(
        SimpleNamespace(graph_command="sync", dry_run=False, apply=True, json=True),
        ledger=ledger,
        driver_factory=lambda: fake_driver,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "apply"
    assert payload["applied"]["upserted"] == 3
    assert len(fake_driver.session_obj.edges) == 3
    assert payload["validation"]["active_krishna_preference_edges"] == 1
    assert payload["validation"]["active_krishna_preference_ok"] is True

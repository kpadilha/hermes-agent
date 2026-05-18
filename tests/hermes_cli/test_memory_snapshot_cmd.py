import json
from pathlib import Path
from types import SimpleNamespace

from agent.memory_ledger import BeliefLedger, MemoryWriteGate
from hermes_cli.memory_snapshot_cmd import build_snapshot_status_report, memory_snapshot_command


class FakeMemory:
    def __init__(self, path):
        self.path = Path(path)
        self.put_calls = []
        self.committed = False
        self.verified = False

    def put(self, **kwargs):
        self.put_calls.append(kwargs)
        self.path.write_bytes(b"fake-mv2")
        return "frame-1"

    def commit(self):
        self.committed = True

    def verify(self, deep=False):
        self.verified = True
        return {"ok": True, "deep": deep}

    def stats(self):
        return {"frames": len(self.put_calls)}

    def find(self, query, k=3):
        return SimpleNamespace(hits=[SimpleNamespace(text="Krishna prefers local-first memory.")])

    def close(self):
        pass


class FakeMemvidSDK:
    def __init__(self):
        self.created = []

    def create(self, filename, *, kind="basic", enable_vec=False, enable_lex=True, **kwargs):
        memory = FakeMemory(filename)
        self.created.append({"filename": filename, "kind": kind, "enable_vec": enable_vec, "enable_lex": enable_lex, "memory": memory})
        return memory


def test_memory_snapshot_create_writes_mv2_and_markdown_metadata(tmp_path, capsys):
    ledger = BeliefLedger(tmp_path / "ledger.db")
    gate = MemoryWriteGate(ledger)
    gate.evaluate_and_record(
        target="user",
        content="Krishna prefers local-first memory.",
        source="test",
        evidence_ref="test#snapshot",
    )
    sdk = FakeMemvidSDK()
    output = tmp_path / "memory-ledger.mv2"

    memory_snapshot_command(
        SimpleNamespace(
            snapshot_command="create",
            output=str(output),
            query="local-first",
            json=True,
            enable_vec=False,
        ),
        ledger=ledger,
        memvid_sdk=sdk,
    )

    result = json.loads(capsys.readouterr().out)
    metadata = tmp_path / "memory-ledger-mv2.md"
    assert result["success"] is True
    assert result["output"] == str(output)
    assert result["metadata_wrapper"] == str(metadata)
    assert output.exists()
    assert metadata.exists()
    assert sdk.created[0]["enable_lex"] is True
    assert sdk.created[0]["memory"].committed is True
    assert sdk.created[0]["memory"].verified is True
    text = metadata.read_text(encoding="utf-8")
    assert "# Memvid Memory Snapshot" in text
    assert "Krishna prefers local-first memory." in text
    assert "local-first" in text


def test_build_snapshot_status_report_finds_latest_mv2_and_wrapper(tmp_path):
    old_snapshot = tmp_path / "old.mv2"
    old_wrapper = tmp_path / "old-mv2.md"
    latest_snapshot = tmp_path / "memory-ledger-20260424.mv2"
    latest_wrapper = tmp_path / "memory-ledger-20260424-mv2.md"
    old_snapshot.write_bytes(b"old")
    old_wrapper.write_text("old wrapper", encoding="utf-8")
    latest_snapshot.write_bytes(b"latest")
    latest_wrapper.write_text("latest wrapper", encoding="utf-8")
    old_time = 1_700_000_000
    latest_time = 1_700_010_000
    current_time = latest_time + 3600
    for path in (old_snapshot, old_wrapper):
        path.touch()
        import os
        os.utime(path, (old_time, old_time))
    for path in (latest_snapshot, latest_wrapper):
        import os
        os.utime(path, (latest_time, latest_time))

    report = build_snapshot_status_report(snapshot_dir=tmp_path, current_time=current_time)

    assert report["success"] is True
    assert report["latest_snapshot"]["path"] == str(latest_snapshot)
    assert report["latest_snapshot"]["age_seconds"] == 3600
    assert report["latest_snapshot"]["size_bytes"] == len(b"latest")
    assert report["wrapper"]["present"] is True
    assert report["wrapper"]["path"] == str(latest_wrapper)
    assert report["freshness"]["status"] == "fresh"
    assert any(item["code"] == "raw_mv2_requires_nas_or_rsync_backup" for item in report["recommendations"])


def test_build_snapshot_status_report_warns_when_wrapper_missing(tmp_path):
    snapshot = tmp_path / "memory-ledger.mv2"
    snapshot.write_bytes(b"mv2")

    report = build_snapshot_status_report(snapshot_dir=tmp_path, current_time=snapshot.stat().st_mtime + 10)

    assert report["latest_snapshot"]["path"] == str(snapshot)
    assert report["wrapper"]["present"] is False
    assert report["wrapper"]["expected_path"] == str(tmp_path / "memory-ledger-mv2.md")
    assert any(item["code"] == "memvid_wrapper_missing" for item in report["recommendations"])


def test_build_snapshot_status_report_handles_missing_snapshot_dir(tmp_path):
    report = build_snapshot_status_report(snapshot_dir=tmp_path / "missing", current_time=1_700_000_000)

    assert report["success"] is True
    assert report["latest_snapshot"] is None
    assert report["wrapper"]["present"] is False
    assert report["freshness"]["status"] == "missing"
    assert any(item["code"] == "memvid_snapshot_missing" for item in report["recommendations"])


def test_memory_snapshot_status_command_outputs_json(tmp_path, capsys):
    snapshot = tmp_path / "memory-ledger.mv2"
    wrapper = tmp_path / "memory-ledger-mv2.md"
    snapshot.write_bytes(b"mv2")
    wrapper.write_text("wrapper", encoding="utf-8")

    memory_snapshot_command(
        SimpleNamespace(snapshot_command="status", dir=str(tmp_path), json=True),
    )

    result = json.loads(capsys.readouterr().out)
    assert result["success"] is True
    assert result["latest_snapshot"]["path"] == str(snapshot)
    assert result["wrapper"]["present"] is True

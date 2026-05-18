from pathlib import Path

from hermes_cli.memory_paths import default_kb_root, default_memory_snapshot_dir
from hermes_cli.memory_snapshot_cmd import _default_snapshot_dir


def test_default_memory_snapshot_dir_uses_os_account_home_when_home_is_profile_sandbox(monkeypatch):
    monkeypatch.setenv("HOME", "/tmp/hermes-profile-home")

    path = default_memory_snapshot_dir()

    assert str(path).endswith("/obsidian-vault/Krishna/niko/operations/memory-snapshots")
    assert not str(path).startswith("/tmp/hermes-profile-home/")
    assert Path(path).is_absolute()


def test_default_kb_root_uses_os_account_home_when_home_is_profile_sandbox(monkeypatch):
    monkeypatch.setenv("HOME", "/tmp/hermes-profile-home")

    path = default_kb_root()

    assert str(path).endswith("/obsidian-vault/Krishna/kb")
    assert not str(path).startswith("/tmp/hermes-profile-home/")
    assert Path(path).is_absolute()


def test_memory_snapshot_default_dir_uses_canonical_memory_path(monkeypatch):
    monkeypatch.setenv("HOME", "/tmp/hermes-profile-home")

    assert _default_snapshot_dir() == default_memory_snapshot_dir()

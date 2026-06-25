"""Path helpers for Hermes memory-adjacent operational artifacts."""

from __future__ import annotations

import os
from pathlib import Path


def os_account_home() -> Path:
    """Return the operating-system account home, not Hermes' profile sandbox HOME."""
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path.home()


def default_kb_root() -> Path:
    """Canonical Krishna KB root, independent of profile/worker HOME sandboxes."""
    return os_account_home() / "obsidian-vault" / "Krishna" / "kb"


def default_memory_snapshot_dir() -> Path:
    """Canonical Obsidian-syncable Memvid snapshot directory."""
    return os_account_home() / "obsidian-vault" / "Krishna" / "niko" / "operations" / "memory-snapshots"

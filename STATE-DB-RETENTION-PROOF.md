# state.db retention proof (2026-10-31)

## Finding and patch

Production config has `sessions.auto_prune=false`, `sessions.retention_days=90`; therefore the existing two-window native maintenance is disabled. It already sweeps stale state-owned `api_server` rows, then deletes ended rows only after a further retention window. The sweep was closing *all* old open API rows, including rows with `sessions.session_key`; those keys support resumable API conversations. Patch adds a narrow opt-out for keyed API rows to the existing sweep. Unkeyed per-request rows retain the existing close-then-retain-then-prune lifecycle. No production config or data was changed.

Regression: `test_auto_maintenance_reaps_only_unkeyed_api_sessions`; proven red on the base (closed 2 instead of 1), green after patch.

## Measured safe preview

Controller's verified SQLite online backup: `/home/krishna/.hermes/archive/state-db-repair-20261031/before.sqlite3`, 5,003,751,424 bytes, `quick_check=ok`, 62,183 sessions / 540,147 messages / 13,625 prompts / 14,645 state_meta rows (receipt: `backup-receipt.json`). Opened backup read-only with `SessionDB`; native `list_prune_candidates(older_than_days=30)` selected 7,199 ended sessions: cron 6,411; Discord 265; Telegram 238; subagent 100; CLI 147; others 38. Those rows contain 798,669,087 bytes of message content, including 583,527,800 bytes in tool/tool_result-role messages. This is a logical payload forecast, not guaranteed filesystem savings (FTS, row/index overhead, free pages differ). Native preview found only 1 ended API row. Separately, the operator's RO inventory counted 45,322 API rows >30d with no key/chat/parent and 8,449 in the last 30d; these are open, hence correctly absent from prune preview. No production deletion or VACUUM was run.

## Supported bounded reclaim sequence (operator-run only)

1. Keep the verified online backup and receipt; do not use the backup as a writable target.
2. Before deletion, export the exact ended-session candidate set to a protected local archive (full JSONL preserves raw content):
   `hermes sessions export /protected/archive/state-db-over-30d.jsonl --older-than 30`
   Preview first with `hermes sessions export - --older-than 30 --dry-run` (or native `hermes sessions prune --older-than 30 --dry-run`). Protect the archive as sensitive data; do not share it.
3. Re-check candidates with `hermes sessions prune --older-than 30 --dry-run`. Only after export verification and explicit operator execution, prune with `hermes sessions prune --older-than 30 --yes`; this acts on ended rows only and respects lineage/pins.
4. If ongoing automatic bounds are approved, enable the native setting (`/autoprune on` or `sessions.auto_prune: true`, retention remains 90 days). It only runs at startup; the code patch prevents stale keyed API sessions from being closed. An unkeyed stale API session is closed after 90 idle days, remains resumable for another retention window, and only then becomes prune-eligible. Do not force VACUUM with live holders; use native `hermes sessions optimize` only when its holder guard allows it.

## Verification

- Focused: `scripts/run_tests.sh tests/hermes_state/test_sweep_orphaned_sessions.py -k auto_maintenance_reaps_only_unkeyed_api_sessions` — 1 passed.
- Broader attempted command: `scripts/run_tests.sh tests/hermes_state/test_sweep_orphaned_sessions.py tests/hermes_state/test_hermes_state.py` hit the 300s timeout; many existing cases failed the real-home I/O guard during bootstrap recovery. No broad-green claim.
- `git diff --check` clean.

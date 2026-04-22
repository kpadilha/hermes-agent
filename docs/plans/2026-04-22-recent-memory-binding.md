# Recent-Turn Memory Binding Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Prevent short confirmation messages (`sim`, `faça`, `continue`, `desenhe`) from drifting to semantically adjacent older context instead of binding to the immediately preceding actionable proposal.

**Architecture:** Add a small deterministic recent-turn binding layer inside `AIAgent.run_conversation()`. On each turn, inspect the transcript tail and, when the new user message is a short confirmation, inject an API-only continuity hint anchored to the last assistant actionable message. Keep the persisted transcript unchanged via `persist_user_message`. This is an MVP for the broader working-memory redesign.

**Tech Stack:** Python, pytest, Hermes `AIAgent`, gateway/session transcript history.

---

### Task 1: Inspect the current turn assembly path

**Objective:** Confirm where to inject recent-turn continuity without mutating durable transcript storage.

**Files:**
- Read: `run_agent.py`
- Read: `gateway/session.py`
- Read: `tests/`

**Step 1: Verify conversation assembly path**
- Confirm `run_conversation()` copies `conversation_history`, appends current user message, and uses `persist_user_message` as the clean transcript form.

**Step 2: Identify insertion point**
- Choose the point before `user_msg = {"role": "user", "content": user_message}` so API-visible content can be enriched while persisted transcript stays unchanged.

**Step 3: Commit decision to plan execution notes**
- Use helper methods on `AIAgent` rather than gateway/session store for the MVP.

---

### Task 2: Write failing tests for recent-turn binding helpers

**Objective:** Lock the desired behavior before implementation.

**Files:**
- Create: `tests/test_recent_turn_binding.py`

**Step 1: Write failing tests for short-confirmation detection**
Cases:
- `sim`
- `SIM dwsenhe`
- `continue`
- non-confirmation text should be false

**Step 2: Write failing tests for assistant-actionable extraction**
Cases:
- direct actionable assistant message should be extracted
- non-actionable assistant chatter should be ignored
- last assistant message wins over older ones

**Step 3: Write failing tests for continuity hint generation**
Cases:
- short confirmation + actionable prior assistant message => hint injected
- no prior assistant actionable => original user text unchanged
- long user message => unchanged
- tool messages / system messages in history should be ignored safely

**Step 4: Run targeted tests and verify failure**
Run: `pytest tests/test_recent_turn_binding.py -q`
Expected: FAIL

---

### Task 3: Implement minimal recent-turn binding in `run_agent.py`

**Objective:** Add deterministic helper methods and apply them before the current user message is appended.

**Files:**
- Modify: `run_agent.py`

**Step 1: Add helper for short confirmation detection**
Suggested helper:
- `_is_short_confirmation_message(text: str) -> bool`

**Step 2: Add helper for finding recent assistant actionable text**
Suggested helper:
- `_extract_recent_assistant_actionable(messages: List[Dict[str, Any]]) -> Optional[str]`

Rules:
- inspect transcript tail in reverse
- only assistant role
- ignore tool/system
- require non-empty textual content
- prefer the immediately preceding assistant message if plausible

**Step 3: Add helper for API-only continuity enrichment**
Suggested helper:
- `_apply_recent_turn_binding(user_message: str, conversation_history: List[Dict[str, Any]] | None) -> str`

Behavior:
- if current user message is a short confirmation and a recent assistant actionable exists,
  prepend/append a compact continuity note for the model
- otherwise return original message unchanged

**Step 4: Use helper in `run_conversation()`**
- preserve original user text for transcript persistence
- enrich only the API-visible `user_message`

---

### Task 4: Verify green and guard regressions

**Objective:** Ensure tests pass and existing behavior is not obviously broken.

**Files:**
- Modify if needed: `tests/test_recent_turn_binding.py`

**Step 1: Run targeted tests**
Run: `pytest tests/test_recent_turn_binding.py -q`
Expected: PASS

**Step 2: Run nearby regression tests if present**
Suggested:
- `pytest tests/gateway/test_unknown_command.py -q`
- any focused `run_agent`/gateway tests touching `run_conversation` message handling

**Step 3: Review diff for scope control**
Run: `git diff --stat`
Expected: narrow change set

---

### Task 5: Persist findings to KB and sync

**Objective:** Capture the implemented MVP and its limitations.

**Files:**
- Update: `~/obsidian-vault/Krishna/niko/operations/hermes-recent-memory-redesign-2026-04-22.md`
- Optional update: `~/obsidian-vault/Krishna/kb/wiki/operations/hermes-memory-architecture.md`

**Step 1: Record implemented MVP**
- note that the first shipped fix is API-only recent-turn binding inside `AIAgent`
- note that full session-scoped structured working memory remains future work

**Step 2: Sync Obsidian**
Run: `PATH="$HOME/.npm-global/bin:$PATH" ob sync`
Expected: success

---

## Acceptance criteria

- Short confirmation messages no longer behave like vague new requests by default.
- The agent preferentially anchors them to the immediately preceding assistant actionable text.
- Persisted transcript keeps the user’s original message, not the injected continuity hint.
- Tests exist and pass.
- KB note updated with MVP scope and remaining gap.

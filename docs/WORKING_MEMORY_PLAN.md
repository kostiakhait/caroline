# Working memory: Notes read-cache + a deliberate short-term fact cache

## Context

Two related but separate problems, clarified through discussion with the user:

1. **Notes are re-fetched from the network on every read** -- `notes_api.py`'s `_read_index`/
   `_read_note_file` (and everything built on them: `list_notes`, `search_notes`, `get_note` in
   `notes_plugin.py`, and `recall_memory` in `memory_search_plugin.py`, which calls `list_notes`
   on literally every invocation) hit Camerlengo fresh every single time, with zero caching
   anywhere today. This is a pure redundant-network-round-trip problem, Notes/SquirrelWisdom-
   account-specific by construction.

2. **"Frequently/recently used information" is a separate, source-agnostic concept** --
   frequently-needed facts (credentials, contacts, etc.) don't only come from Notes, and Notes may
   not exist at all for a given install (no SW account). This needs its own mechanism, independent
   of Notes, that Caroline populates **deliberately** (not a transparent/automatic cache of API
   responses) and that auto-injects into every system prompt -- which is exactly why it needs real
   eviction: an unbounded injected cache would pollute every prompt.

Both land on the same precedent already established in this codebase for exactly this kind of
always-injected-but-must-stay-tiny content: `app/owner_profile.py` (local JSON file in the
workspace dir, a system-prompt clause function wired into both `chat_session.py` and
`small_model_engine.py` right next to `persona_system_prompt_append`/`_persona_system_message`,
and a short always-on *trigger* in `policies.py` pointing at a plugin whose full mechanics are
on-demand via `get_tool_instructions` -- same split `recall_memory_check_first_instruction` uses).

## Part A -- Notes read-through cache (`notes_api.py`)

Scope: process-wide (mirrors `SessionManager`, already shared across every tab/account), in-memory
only (no new local file -- a stale-on-restart cache is fine, Notes data isn't local-only state).

- New small cache inside `notes_api.py` (next to `SessionManager`, same file): keyed by
  `hash16(account) -> {"index": (data, fetched_at), "notes": {note_id: (data, fetched_at)}}`.
- `_read_index`/`_read_note_file` check the cache first; a hit within `NOTES_CACHE_TTL_S` (180s --
  long enough to kill `recall_memory`'s repeated full-index refetch within one burst of tool
  calls, short enough that an edit from the web portal/phone is never stale for long) returns the
  cached copy; a miss fetches and stores.
- Write-through invalidation: `_write_note_file`/`_patch_index` (and therefore every write path:
  `_save_note`, `create_note`, delete, attachments) clear that account's cached index entry (and
  the specific note's cached body, if it's a body write) immediately after a successful write --
  so Caroline's own edits are never stale, only external ones are TTL-bounded.
- No change needed to `notes_plugin.py`/`memory_search_plugin.py` themselves -- they already call
  through `notes_api.py`'s functions, so the cache is transparent to every caller.
- Gracefully inert when there's no SW account: the cache simply never gets populated (every call
  already raises/handles "not logged in" upstream, unchanged).

## Part B -- Deliberate short-term fact cache (new, source-agnostic)

### Categories
`credentials`, `contacts`, `commands`, `references`, `events`, `facts` (catch-all).

### Storage: new `app/working_memory.py`, same local-JSON-in-workspace-dir shape as `owner_profile.py`
- File: `<workspace_dir>/working_memory.json` -- `{category: [{"key", "value", "created_at",
  "last_used_at", "use_count"}, ...]}`.
- `MemoryFact` dataclass (key, value, created_at, last_used_at, use_count).
- `remember_fact(workspace_dir, category, key, value) -> MemoryFact` -- upsert by `(category,
  key)`: existing key gets its value replaced and `last_used_at=now`, `use_count += 1`; a new key
  starts at `use_count=1`. Runs eviction after insert (see below).
- `touch_fact(workspace_dir, category, key) -> MemoryFact | None` -- bumps `last_used_at`/
  `use_count` **without** restating the value, for "I actually used this again" without a
  redundant write -- this is what keeps the frequency signal meaningful (counts real reuse, not
  passive presence in a prompt the model never even acted on).
- `forget_fact(workspace_dir, category, key) -> bool` -- explicit removal (e.g. a credential that
  no longer works).
- `list_facts(workspace_dir, category=None) -> list[MemoryFact]` -- for a listing tool, so Caroline
  can see what's cached without relying purely on the injected prompt block.

### Eviction (hybrid LFU+LRU, with a hard size cap)
- Per-category cap: `MAX_PER_CATEGORY = 8`. On overflow, evict the entry with the lowest
  `use_count`; ties broken by oldest `last_used_at`.
- Global render-size cap: `MAX_TOTAL_CHARS = 2000` -- small and fixed, same bounded-size reasoning
  `owner_profile_system_prompt_clause` already documents re: the 2026-09-26
  `--append-system-prompt` overflow incident. If the rendered block would exceed this after the
  per-category cap, keep evicting globally-lowest-`use_count`/oldest entries (across all
  categories) until it fits -- the per-category cap bounds breadth, this bounds absolute cost.
- `working_memory_system_prompt_clause(facts_by_category) -> str` -- returns `""` when empty (no
  nudge text needed here, unlike `owner_profile`'s unset-state clause -- this cache is purely
  opportunistic, there's nothing Caroline is obligated to go looking for). When non-empty, renders
  a compact, category-grouped block, e.g.:
  ```
  Things you've chosen to keep handy (update via remember_fact, remove via forget_fact if stale):
  [credentials] gmail SMTP: user=..., pass=...
  [contacts] Oksana's lawyer: email=..., phone=...
  ...
  ```

### Plugin: new `app/plugins/working_memory_plugin.py`
Tools: `remember_fact(category, key, value)`, `touch_fact(category, key)`, `forget_fact(category,
key)`, `list_remembered_facts(category=None)`. `category` validated against the fixed 6-value set.
On-demand `usage_instructions` (fetched via `get_tool_instructions`, not always-on) explain: what
each category is for, that `touch_fact` (not re-calling `remember_fact` with the same value) is how
to mark real reuse, and that eviction is real and silent -- a fact can disappear if it stops being
used, so don't treat this as durable storage (durable belongs in Notes, same distinction already
drawn between `Caroline:Topics` and this).

### Always-on trigger (short, in `policies.py`, same shape as `recall_memory_check_first_instruction`)
New `working_memory_check_first_instruction()`: one short paragraph -- before re-deriving/
re-fetching a credential, contact, command, or reference you've needed before this session, check
here first; when you find yourself using something a second time (or expect to need it again soon),
save it with `remember_fact` -- added to `ALWAYS_ON_INSTRUCTIONS` in `policies.py` and to
`small_model_engine.py`'s `_SHARED_ALWAYS_ON_INSTRUCTIONS`, matching how every prior always-on
trigger in this codebase requires both-engine parity.

### Wiring (same two call sites as `owner_profile`, same order -- right after it)
- `chat_session.py` system_prompt_parts: add
  `working_memory_system_prompt_clause(load_working_memory(self.workspace_dir))` immediately after
  the existing `owner_profile_system_prompt_clause(...)` line.
- `small_model_engine.py`'s `system = "\n\n".join([...])` list: same addition, same relative
  position as `owner_profile_system_prompt_clause` was added there.

### Security note (flagged, not blocking)
`working_memory.json` stores plaintext JSON on local disk, same posture as `persona.json`/
`owner_profile.json`/`settings.json` already in this codebase -- no new precedent, but worth
explicit awareness since the `credentials` category specifically means real secrets sitting in a
local file for as long as they stay "hot" (bounded by the eviction cap, not by time). No
encryption-at-rest is proposed here; if that's wanted it's a deliberate follow-up, not bundled in.

## Critical files

- `backend-py/app/plugins/notes_api.py` -- Part A (read-cache + write-invalidation).
- `backend-py/app/working_memory.py` (new) -- Part B storage/eviction/clause, mirrors
  `app/owner_profile.py`'s shape closely.
- `backend-py/app/plugins/working_memory_plugin.py` (new) -- Part B tool surface, mirrors
  `app/plugins/owner_profile_plugin.py`'s shape (get/set-style tools, on-demand usage_instructions).
- `backend-py/app/policies.py` -- new `working_memory_check_first_instruction`, added to
  `ALWAYS_ON_INSTRUCTIONS`.
- `backend-py/app/small_model_engine.py` -- `_SHARED_ALWAYS_ON_INSTRUCTIONS` parity + the
  `system = [...]` list addition.
- `backend-py/app/chat_session.py` -- the `system_prompt_parts` list addition.

## Verification plan

- Part A: a standalone script that reads the same note twice in quick succession and confirms the
  second call hits the cache (no second network call -- confirmed via a log line or timing); then
  writes the note and confirms the next read is fresh, not stale-cached.
- Part B: a standalone script exercising `remember_fact`/`touch_fact`/`forget_fact`/`list_facts`
  directly against a scratch workspace dir, confirming: upsert semantics, per-category eviction at
  9 inserts into one category, global char-budget eviction with deliberately oversized values, and
  that `working_memory_system_prompt_clause` returns `""` on a fresh/empty store.
- Live: ask Caroline to remember a made-up credential, start a new turn (so it's read from the
  file, not conversation memory) and confirm she references it correctly; confirm `recall_memory`/
  Notes reads feel faster on a repeated query in the same session (Part A).

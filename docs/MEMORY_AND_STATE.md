# Memory and persisted state

Caroline has several kinds of memory with different lifetimes and owners. The rule that ties them
together: **nothing important may live only in a process.**

## Kinds of memory

| Kind | Where it lives | Written by | Read by |
| --- | --- | --- | --- |
| **Long-term facts and topics** | Server side (SquirrelWisdom microagent memory: `FactMemory`, `TopicMemory`, `DocumentMemory`, `AddressBook`) | `save_info` (`plugins/memory_plugin.py`) | `request_info`, the memory advisor |
| **Topic summary cache** | `workspace/memory_topics.json` (`memory_topics.py`) | After each save | System prompt: topics of the last 24 h and the last week; older ones only through `request_info` |
| **Standing instructions** | `workspace/instruction_memory.json` (`instruction_memory.py`) | `instruction_memory_plugin` | Every system prompt of both engines; ranked by priority, then use count (LFU), then recency; lowest rank evicted when full |
| **Owner quick profile** | `workspace/owner_profile.json` (`owner_profile.py`) | `owner_profile_plugin` | System prompt (name, gender, age only). The full profile lives in the Notes folder `Caroline:Profile` |
| **Working memory** | `workspace/working_memory.json` (`working_memory.py`) | Deprecated tool, kept read-only | Prompt block; small, auto-evicting cache |
| **Event memory** | `workspace/event_memory.json` (`event_memory.py`) | `event_memory_plugin` | Durable record of things Caroline arranged (meetings, reminders) |
| **Action ledger** | `workspace/action-ledger.jsonl` (`action_ledger.py`) | Code, when a send-type tool finishes | Prompts and checks: what was actually *done* on the user's behalf |
| **Dialogue log** | `workspace/` per-tab (`dialogue_log.py`) | Code | What the user was actually *shown* (after reply review, translation, silence rules) |
| **Transcript journal** | `workspace/transcript-journal/` (`transcript_journal.py`) | Code, append-only | Restore of the chat window when its localStorage blob is lost |
| **SW history** | `workspace/sw-history/<session>/` (`sw_history.py`) | SW cascade | The cascade's memory across turns (tool calls and a rolling summary) |
| **Agent registry** | per tab (`agent_registry.py`) | Launch of a background agent | After compaction or restart: which agents were launched and are gone |
| **Skills** | `workspace/Skills/` (seeded from `backend-py/skills-src/`, plus `lessons-learned/` she writes herself) | `skills_seed.py`, the model | Catalog in the prompt; `use_skill` / `read_skill_file` |

Deprecated and read-only (kept so nothing they hold is lost): `recall_memory`, `working_memory_*`,
`topic_upsert` / `topics_list` / `topic_close` (`deprecated_memory.py`).

### `save_info` / `request_info`

* `save_info` completes server-side in tens to hundreds of seconds. To make that restart-safe,
  `memory_save_queue.py` keeps a durable queue (`workspace/memory_save_queue.json`): a crash or a
  restart no longer drops the follow-up (local topic cache update, failure notification).
* `memory_turns.py` assigns a turn id to every memory request: the server lets a turn that started
  with money in the wallet finish even if the wallet empties during it.
* `memory_nudge.py` counts plugin tool calls since the last `save_info` and reminds the model
  every N calls (an 80-call browser task once saved nothing).
* `memory_advisor.py` runs `sw.memory.need` before every real user message and, when the answer
  may depend on what Caroline knows, performs the search in code and appends the result as an
  block starting `[Memory look-up done for this message` (`memory_advisor.BLOCK_HEADER`). It
  fails open: an advisor error means "memory is needed" and the message itself is the query.

## The workspace directory

`%LOCALAPPDATA%\Caroline\workspace\` (override `CAROLINE_WORKSPACE_DIR`; resolved once in
`workspace_dir.py`). All tabs share it. Files and folders the backend creates (non-exhaustive):

| Path | Purpose |
| --- | --- |
| `tab-session-<tab>.json` | Resume id of the tab's engine session (one per answer source) |
| `pending-turn-<tab>.json` | A received, not yet answered user turn |
| `tab-continuity-<tab>.json` | Pointer to the archive of a session reset as unrecoverable |
| `schedule.json` | Reminders (`plugins/scheduler_plugin.py`) |
| `persona.json`, `visualMode.json`, `visual-model-override.json`, `continuousDialogue.json`, `local-stt-settings.json`, `wizard.json`, `subscription.json` | Settings; mirrored to Notes by `settings_mirror.py` |
| `dehydrated/`, `cold/` | Archived transcripts, chunked with an index |
| `uploads/`, `telegram_downloads/`, `whatsapp_downloads/`, `signal_downloads/` | Received files |
| `messengers/<service>/` | Messenger session state, DPAPI-encrypted on Windows (`secret_store.py`) |
| `web_browser_profile/` | Profile of the headless Chromium used for web research |
| `openai-transcripts/` | Codex transcripts |
| `Skills/` | Skills (see above) |

Outside the workspace: the Claude CLI's own transcripts in `~/.claude/projects/<slug>/*.jsonl`
(the slug replaces every non-alphanumeric character by `-`), SquirrelWisdom credentials in
`~/.mcp-notes/credentials.json`, Codex state in `~/.codex` (or `CAROLINE_CODEX_HOME`).

## Backup and restore to Notes

* **Settings mirror** (`settings_mirror.py`): every plain setting file is one note
  `settings:<name>` in Notes folder `Caroline:Settings`; secrets go to `vault:settings:<name>` in
  `Caroline:Vault`. A reinstall or a new machine reads them back instead of asking again.
  Folder names are overridable with `CAROLINE_SETTINGS_FOLDER` / `CAROLINE_SETTINGS_VAULT_FOLDER`.
* **Vault backup** (`vault_backup.py`): the hourly backup of the persona, schedule and other memory
  files, `Skills/lessons-learned/*/SKILL.md` (written only when the hash changed) and
  `dehydrated/` + `uploads/` attachments is done by code, with no model call (it used to be an hourly
  Claude turn). Restore on a new machine is code too.
* Secrets the user mentions in chat are saved as a note in the Notes `vault` folder, never in local
  files, chat history or skills (policy enforced through the system prompt, `policies.py`).
* `archive_prune.py` removes redundant full-transcript copies in `dehydrated/` (a past bug piled
  up thousands of byte-for-byte prefixes, hundreds of GB).

## Data handled as untrusted

Anything written by another person (a messenger message, an e-mail body, a web page) is wrapped by
`untrusted_text.py` in markers it cannot forge before it reaches the model, because the model has
tools that run commands and send files. Files leaving or entering the machine are judged by the
`turn.file.transfer` microagent using plain facts from `file_facts.py` (no static folder blacklist).

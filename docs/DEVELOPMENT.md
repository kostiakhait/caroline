# Development guide

## Layout you will touch

* Backend: `backend-py/` (Python 3.10+ on Windows with the bundled runtime, 3.12 in the Linux
  runtime). UI: `Windows/Caroline/wwwroot/` (plain `chat.html` + `chat.js`, no bundler). Shells:
  `Windows/Caroline/` (C#), `linux-shell/` (Python/GTK).
* The backend never imports the server repository; helper modules come from
  `backend-py/camerlengo-vendor-stubs` (or a sibling `reforce` checkout, see [BUILD_AND_RELEASE.md](BUILD_AND_RELEASE.md)).

## Running the backend

```bash
cd backend-py
python run_server.py          # listens on 127.0.0.1:48765 (and 0.0.0.0:48768)
```

Use the same entry point as production (`run_server.py`), not `python -m app.main`. Point
`CAROLINE_WORKSPACE_DIR` at a scratch folder to keep your real workspace untouched. A SquirrelWisdom
login (or a Claude/Codex sign-in) is needed for anything that calls a model; use the wizard at
`http://127.0.0.1:48765/wizard` or Settings. Without a shell you can drive a tab with
`POST /api/message` and watch `GET /api/status`, or connect a WebSocket to `/?tab=1`.

The full app: `make build`, then run `dist/Caroline.exe` (it starts the supervisor, which starts the
backend). Because plugins are re-imported for new files on every turn, a **new** plugin file is
live within a turn; an edit to an already-imported module needs a backend restart.

## Tests

About 49 `test_*.py` files sit directly in `backend-py/`, run with pytest from that directory:

```bash
cd backend-py
python -m pytest -q                         # everything
python -m pytest test_permission_policy.py -q
```

They are unit tests with mocked model calls and notes (for example `test_memory_advisor.py`,
`test_vault_backup.py`, `test_claude_ladder.py`, `test_compaction_guard.py`, `test_release_manifest.py`,
`test_overlay_install.py`, `test_pseudo_tab_retry.py`). Tests exercising live models are not part of the
suite; `backend-py/evals/` holds scripts for that (`verifier_eval.py`, `stt_sanity_eval.py`,
`narrator_eval.py` + `narrator_golden.py`, `file_transfer_eval.py`, `bench_fast_lane.py`,
`model_calls_via_server_check.py`). `docs/SW_MODE_TESTING_PLAN.md` describes the live test setup for
the SW mode. UI-level behaviour is checked by hand and with screenshots, so test through the real
window or the HTTP/WebSocket surface, not only through unit tests.

## Conventions seen in the code

* **Dated, incident-driven comments.** A non-obvious guard carries the date and the incident that
  caused it. Keep that habit; when removing a guard, read the incident first.
* **Models decide fuzzy things.** Do not add keyword lists or string comparison for meaning; add or
  adjust a microagent and give it facts from code ([MICROAGENTS.md](MICROAGENTS.md)).
* **Fail visibly.** A background failure must log (JSON-lines `log_event(component, event, ...)`);
  a user-visible failure must say what happened. Bare `except: pass` is how several outages hid.
* **No blocking in the event loop.** Anything slow (certificate loading, transcript walks, large
  JSON parsing) goes to a thread or a separate process; `loop_watchdog.py` will name the culprit.
* **Bound everything the model reads** (`context_cap.py`), and treat third-party text as data
  (`untrusted_text.py`).
* **Windows paths and command lines:** the `CreateProcess` limit (~32K chars) once broke every tab
  because a profile was inlined into the system prompt. Keep the appended prompt small.
* **Do not hard-code tool or server names** to decide whether something is "one of ours"; ask
  `discover_plugins()`.
* **Edition cuts:** code only the Claude/OpenAI sources need is wrapped in `edition:cut` markers
  (see `tools/edition_build.py`); keep new engine-specific code inside them.
* **Language:** user-facing strings and calibrated prompts are partly Russian; wording in
  `policies.py` is "calibrated, incident-driven phrasing", change it only with a reason.

## Debugging checklist

1. `caroline.log`: find the tab (`tab_id=`) and the event (`engine`, `ws`, `plugin_*`,
   `memory_advisor_*`, `resuming_unfinished_turn`, `submit`, ...).
2. Event loop stalls: look for the `loop_watchdog` stack dumps.
3. A tool "does not exist": check `plugin_import_failed` (a missing platform dependency), and try
   `CAROLINE_FLAT_TOOLS=1` to rule out the group layer.
4. A tab stuck "reconnecting": failure classification and restart-budget log lines
   (`restart_backoff`), then `process_activity` for the CLI process.
5. A turn replayed or dropped after restart: inspect `pending-turn-<tab>.json` and
   `tab-session-<tab>.json` in the workspace.
6. Visible history wrong: compare `transcript-journal/`, the `dialogue_log` and the CLI `.jsonl`
   under `~/.claude/projects/<slug>/`.
7. Update trouble: `GET /api/update/status` and `/api/update/activity`; `CAROLINE_UPDATE_DISABLE`
   rules updates out.

## Where decisions are recorded

Plans in this directory record why things are the way they are. Useful starting points:
`MICROAGENTS_PLAN.md` (memory and microagent split, money rules), `STAKES_AWARE_LADDER_PLAN.md`,
`CONTINUOUS_NARRATION_PLAN.md` (voice narration timing), `WORKING_MEMORY_PLAN.md`,
`LINUX_PORT_PLAN.md`, `UPDATE_EVERYWHERE_PLAN.md`, `RELEASE_1_0_0_PLAN.md`.

## Known open areas (from the code and plans)

* The tool-group layer changes what tool names appear in the stream; any new code that inspects
  tool names must unwrap `call_tool`.
* Compaction triggering is time- and growth-based; a cost-aware policy is discussed but not done.
* The Android companion is a skeleton (see its README).
* The update rule is designed for all platforms; check `UPDATE_EVERYWHERE_PLAN.md` for which stages
  are built before assuming a platform auto-updates.

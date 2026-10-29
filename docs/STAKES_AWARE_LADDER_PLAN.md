# Model ladders: the start rung decided by a model, for every engine

- **Author:** Claude (Claude Code), for Konstantin Khait
- **Version:** 0.1 (draft for approval)
- **Date:** 2026-10-28
- **Company:** Partners Solutions
- **Project:** Caroline

## Why

Live, 2026-10-28, tab 1, about 20 minutes on a serious matter (court documents, the correspondence with
the court, a decision the user has to make). The user asked to read the documents and the whole
correspondence and to discuss the prospects. From `caroline.log`:

- **Every one of the 18 turns started on Haiku.** Sonnet ran 3 times, each only *after* the verifier
  caught Haiku's draft asserting facts that were not in the tool output. Opus never ran.
- **The ladder forgets.** A turn that just had to be escalated is followed by a turn that starts on Haiku
  again -- also when the user's message is "you did not read anything", "you got it all wrong".
- The answers the user saw were wrong on the central facts (what the order decided, what the
  correspondence was about), and the user had to correct them one by one.

What decides the model today:

| Engine | Ladder | Start rung |
|---|---|---|
| Claude (`app/claude_ladder.py`) | haiku -> sonnet -> opus | always haiku |
| sw (`small_model_engine.py`) | nano -> mini -> Sonnet -> Opus | max(fast lane / SIMPLE-COMPLEX, `sw.tier.advice`) |
| Codex (`app/engines/codex_engine.py`) | none | the one model of the settings, default effort |

Nothing looks at how much is at stake, at how the previous turns of the same conversation went, or at the
user's reaction. SIMPLE/COMPLEX measures the number of dependent tool steps, not the stakes: "read these
documents and tell me my prospects" can score SIMPLE. `sw.tier.advice` works from statistics by task tags
and says step 0 when there is too little data.

**Instruction (2026-10-28):** on serious questions such as legal ones, the advisor must take the model up
the ladder, in every engine. **The model decides how far up**, from the previous dialogue, the
seriousness of the topic and the user's reaction. No hard-coded topic list, no fixed "legal -> Opus" rule.

## 1. Microagent `turn.rung.advice`

A new client-side microagent (`app/microagents/turn_rung_advice.py`, same shape as `sw_tier_advice.py`:
schema + prompt + code `verify`, run through the local microagent ladder).

**Input**

- `request` -- the user's message this turn (the formalized request where the engine has one).
- `dialogue` -- the recent dialogue of the tab (the same window the verifier gets:
  `_ladder_recent_dialogue` / `recent_dialogue_lines`), with the attachments named.
- `history` -- the last few turns of this tab as the ladder saw them: for each, the rung it started on,
  the rung it ended on, and the verifier's notes when it escalated (section 3).
- `reaction` -- the score of `turn.reaction.score` for this message, when it is ready in time (it already
  runs on every real user message; this is a reuse, not a new call).
- `ladder` -- the engine's steps, cheapest first, each with one line on what it is good for.

**Prompt (what it judges)** -- three things, with the decision left to it:

1. **Stakes:** what a wrong or careless answer would cost the user. Legal, medical, financial, a
   document the user relies on, a decision with consequences -- high; chit-chat, a lookup, a routine
   report -- low. Judged by meaning, not by keywords.
2. **The conversation so far:** a topic where earlier turns had to be escalated, or were corrected by
   the user, needs at least the step that finally managed it.
3. **The user's reaction:** dissatisfaction with the previous answer ("you did not read", "that is not
   what it says") means the previous step was not enough -- go above it.

**Output:** `{"start_rung": <step>, "why": "<one sentence>"}`.

**verify (code):** `start_rung` is an int and one of the ladder's steps; `why` is not empty.

**Failure / timeout:** no advice -- the engine starts where it does today (Claude: haiku; sw: its
existing choice; Codex: the default). Never blocks the turn. Timeout of the same order as
`COMPLEXITY_TIMEOUT_S`, run in parallel with the turn's other preparation, so most turns pay no extra
latency.

Every run is logged (`turn_rung_advice` event: engine, start_rung, why, ms), including the failures.

## 2. Wiring per engine

The advice only ever **raises** the start: it is a floor, the ladders still go up on a failed verdict
as before.

- **Claude** (`chat_session_mixins/ladder.py`, `_ladder_before_yield`): `LadderTurn(user_text=..., rung=advice)`
  instead of always rung 0; the model is set with the existing `_ladder_set_model`. Ladder `continuation`
  turns do not ask again. A model pinned in Settings still pins the model (no ladder, no advice).
  `claude_ladder_turn_started` gets `advised_rung` and `why`.
- **sw** (`run_small_model_turn`): `start_index = max(fast lane / complexity, sw.tier.advice, turn.rung.advice)`.
  The ladder given to the microagent is the sw `tiers`. `small_model_start_tier_raised` is logged with
  `source="rung_advice"`.
- **Codex**: one model, so the ladder is reasoning effort (and, if set in Settings, a stronger model for
  the top step): e.g. `low -> medium -> high -> xhigh`. The advised step goes into `turn/start` for that
  turn (to verify against the Codex app-server: per-turn `effort` / `model` overrides on `turn/start`;
  if not supported, `thread` settings are changed before the turn). With no advice, the turn runs as today.

## 3. The ladder remembers (input for the advice)

Per tab, a short ring of the last turns (about 10): `{ts, start_rung, end_rung, escalation_notes}`,
written by each engine when its turn settles (Claude: `_ladder_on_result` once the verdict is "done" or the
top rung answered; sw: where `tiers_ran` / `closed_on` are recorded; Codex: the effort used). In memory,
mirrored to `workspace/rung-history-<tab>.json` so a restart does not reset it.

This is not a rule ("next turn starts where the last ended"): it is what the microagent reads to decide.
Unrelated new topics can and should go back down.

## Not in this plan (seen in the same logs, separate)

- The Claude-ladder verifier sees each tool result cut to 4,500 characters and the last 40 calls
  (`TRACE_RESULT_CHARS`, `TRACE_LINES_KEPT`). On long documents it cannot really check an answer against
  the source, and its escalation notes say exactly that ("not supported by the tool output provided").
  Worth its own change.
- Downward steps inside a Claude turn (sw has `CAN_DEESCALATE`; the Claude ladder only goes up).

## Steps

1. `app/microagents/turn_rung_advice.py` (schema, prompt, verify) + registration with the local runtime.
2. Rung history per tab (section 3), written by the three engines.
3. Claude wiring (`ladder.py`, `claude_ladder.LadderTurn` start rung) + logging.
4. sw wiring (`run_small_model_turn`) + logging.
5. Codex: check what `turn/start` accepts, then the effort ladder + wiring.
6. Tests: `test_claude_ladder.py` (advised start, failure -> rung 0, pinned model -> no advice), sw start
   index = max of the three, the microagent's `verify`.
7. Live check on the 2026-10-28 conversation shape: a request to read documents on a legal matter must
   start above the bottom rung; "you got it wrong" after an escalated turn must not start below it; a
   plain "what's the weather" must still start at the bottom.

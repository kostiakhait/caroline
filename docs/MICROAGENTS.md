# Microagents (client side)

A microagent is a separately invoked, narrow model call: a strict input schema, a strict output
schema, exactly one job, and code that validates the answer. Caroline uses them wherever a decision
is fuzzy and a prompt instruction is only a request. The conceptual background and incident history
are in `PROJECT.md` and `MICROAGENTS_PLAN.md`; this document is the code-level reference.

## Where they run

| Domain | Executes | Examples |
| --- | --- | --- |
| Memory, documents, address book, search | **Server** (SquirrelWisdom/Camerlengo) | `FactMemory`, `TopicMemory`, `DocumentMemory`, `AddressBook` |
| `reply:` domain and the SW cascade's advisors | **In Caroline's process** (`backend-py/app/microagents/`) | everything in the table below |

Camerlengo is a universal server shared by several products, so product-specific rules never run
there; the client reaches it through one domain-agnostic primitive (`microagent:call`: model class,
prompt, timeout in; text and usage out). Provider keys and billing stay on the server.

## Runtime (`microagent_client.py`)

* `build_local_runtime()` builds the `Microagent.Runtime` (the vendored
  `camerlengo-vendor-stubs/Microagent.py`, an identical copy of the server's file) with one adapter,
  one ladder and one concurrency limit (`_MAX_ACTIVE_AGENTS`) shared by all agents.
* `_llm_call(model_class, prompt, timeout_s, reasoning)` posts to the server with retries; the
  server returns text and usage. `run_timed()` and `_note_llm_call()` record timing.
* **Ladder:** start on `nano`; escalate (`nano` again told what failed, then `mini`) only when the
  answer fails *validation* (malformed structure, failed sanity check), never because an answer was
  merely debatable.
* A failed agent never silently allows a risky action: the callers define the failure mode
  (e.g. `sw.memory.need` failure = "memory is needed"; the permission guard treats an unanswered
  question as a refusal).

## Anatomy of an agent

Each module in `app/microagents/` registers agents through `llm_agents()` returning
`M.Agent(NAME, version, description, input_schema=, output_schema=, build_prompt=, verify=)`:

* `build_prompt(input)` returns the prompt text. Rules text is the agent's *genome* and can be
  evolved (see below).
* `verify(input, output)` returns `None` or an error string; an error triggers the next rung.
* Optional `is_empty` for "nothing to report".

See `permission_guard.py` for a compact example (schema, prompt, verify, `llm_agents`).

## Roster

**`reply:` domain (trust and delivery)**

| Name | Module | Decides |
| --- | --- | --- |
| `turn.permission.identify` | `permission_identify.py` | Pending send/spend and identity; matched against up to 5 open actions |
| `turn.permission.guard` | `permission_guard.py` | Whether the user's OK is on record for what is about to happen |
| `action.sanction` | `action_sanction.py` | After each real user message: did it grant or revoke approval of an open action |
| `turn.action.summary` | `action_summary.py` | One-sentence description of a send-type call |
| `turn.file.transfer` | `file_transfer.py` | Whether a local file may leave the machine / a received file may be written somewhere |
| `reply.review` + `reply.review.check` | `reply_review.py`, `reply_review_check.py` | Outbound text on-topic, free of internal plumbing, human-sounding; rewritten when not; the checker sanitizes the rewrite |
| `reply.translate` | `reply_translate.py` | Translation to the conversation language, preserving deliberate quotes |
| `turn.refusal.guard` | `refusal_guard.py` | Whether the assistant quietly declined a real task |
| `turn.reply.expected` | `reply_expected.py` | Whether a reply is expected after a message sent |
| `turn.question.already_answered` | `question_already_answered.py` | Whether a question just asked was already answered |
| `turn.delivery.completeness` | `delivery_completeness.py` | Whether an outbound message contains what it claims to deliver |
| `turn.background.relay.worth` | `background_relay_worth.py` | Whether a background result is worth interrupting the user |
| `content.summarize` | `content_summary.py` | Compression of long text |
| `reminder.advisor` | `reminder_advisor.py` | Whether an existing reminder already covers a new one |
| `turn.rung.advice` | `turn_rung_advice.py` | Start rung of a turn (stakes, conversation, reaction, rung history) |
| `turn.reaction.score` | `reaction_score.py` | The user's reaction to what Caroline did (input to evolution) |

**SW cascade advisors and checks:** `sw.memory.need`, `sw.tier.advice`, `sw.task.tags`,
`sw.tools.groups` / `sw.tools.calls` (tool advisor), `sw.tool.effect`, `sw.action.scope`,
`sw.account.consistency`, `sw.task.on_behalf`, `sw.content.extract` / `sw.content.grounding`,
`sw.result.evidence` / `sw.result.complete`, `sw.attempt.progress`, `sw.history.compact`,
`sw.turn.tools.summarize`. Web research uses the `web.*` agents in `web_research_agents.py`.

## Action boundaries (`action_registry.py`)

A per-tab, max-5, LRU-evicted registry of open *actions* (a bounded operation such as "send the
renewal e-mail to X"; unrelated to the long-lived *topic* of TopicMemory). `turn.permission.identify`
matches a pending call to an open action on the call it already makes. After each real user message
`action.sanction` reconsiders every open action. An action flagged `approved` skips
`turn.permission.guard` for that call, so a given approval cannot scroll out of a dialogue window
and be asked again. `permission_policy.py` is the *mechanical* half: the model only classifies the
type of operation and on whose behalf; plain code decides whether to allow it.

## Evolution

A role's prompt can improve from real use without touching the main model. For the `reply:`
domain the server only says *which genome to run* (`evolution:resolveCalls`, `mintClone`,
`recordOutcome`); mutation (`mutator.py`, `evolution.mutator`), the champion/challenger run and
judging (`selector.py`, `evolution.selector`) execute locally (`evolution_client.py`,
`evolvable_roles_client.py`, `evolution_request.py`). The judge gets the user's request, the role's
full rules and the last 5 dialogue lines. A role with no settings row, or a dice roll of "not
this time", costs nothing extra. Design: `MICROAGENTS_EVOLUTION_ARCHITECTURE.md`,
`CLIENT_AGENT_EVOLUTION_PLAN.md`.

## Adding a microagent

1. Create `app/microagents/<name>.py` with `NAME = "<domain>.<name>"`, schemas, `build_prompt`,
   `verify` and `llm_agents()`.
2. Make the runtime aware of it: agent modules are imported by the code that owns the domain
   (for the evolvable `reply:` roles, `evolvable_roles_client.py` imports them from
   `app.microagents`). Follow how the nearest existing agent is wired.
3. Call it through the runtime (`microagent_client.run_timed`) from the code that needs the
   decision, with an explicit failure mode and a timeout.
4. Never replace the decision with keyword matching; supply facts from code instead and let the
   agent decide.
5. Test with a mock agent / mock model call (see `test_memory_advisor.py`,
   `test_reminder_advisor.py`, `test_microagent_client.py`).
6. Run the relevant script from `backend-py/evals/` when the agent has one
   (`verifier_eval.py`, `stt_sanity_eval.py`, `narrator_eval.py`, ...).

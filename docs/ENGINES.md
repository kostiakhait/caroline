# Engines, answer sources and the model ladder

A tab answers through exactly one *source* at a time, chosen with the `Claude / SW / OpenAI`
switch in the tab header. Nothing migrates when you switch; each source keeps its own resume state
(`tab-session-<tab>.json`).

| Source | Who pays / whose account | Runs as | Code |
| --- | --- | --- | --- |
| **Claude** | The user's own Claude subscription (`claude auth login`) or a pasted Anthropic API key (`subscription.json`). Never proxied through SquirrelWisdom. | `ClaudeEngine` over `claude_agent_sdk.ClaudeSDKClient` (one CLI process per tab) | `engines/claude_engine.py`, `subscription_mode.py` |
| **OpenAI** | The user's own ChatGPT/Codex sign-in or a separately pasted `OPENAI_API_KEY` | `CodexEngine`: one `codex-app-server` process per tab, JSON-RPC over stdio | `engines/codex_engine.py`, `engines/codex_rpc.py`, `openai_mode.py`, `openai_account.py` |
| **SW** | A SquirrelWisdom wallet (balance must be positive to enter the mode) | The in-process cascade `run_small_model_turn`; models are reached through the service's chat-completions proxy | `small_model_engine.py`, `model_billing.py`, `model_key_provisioning.py` |

`subscription_mode.py` and `openai_mode.py` answer "can this tab use this source right now";
`chat_mode_eligible()` requires a SquirrelWisdom login with a positive balance for SW. With no
Claude account and no SquirrelWisdom login Caroline cannot respond at all (`chatSource == "none"`),
and the wizard / Settings ask for one of them.

## The `AgentEngine` contract (`engines/base.py`)

An engine owns one live agent process for one tab. `ChatSession` feeds user messages through an
async iterable given to `connect()` and consumes `receive_messages()`, which yields
`claude_agent_sdk` message objects (`SystemMessage`, `AssistantMessage`, `UserMessage`,
`ResultMessage`). An engine that is not the Claude SDK adapts its own events to those shapes, so
`wire.py`, failure classification, status and history are engine-agnostic.

* `CodexEngine` translates Codex thread/turn/item notifications into those messages. Caroline's
  tools (the in-process MCP servers built by `plugins/loader.py`) are handed to Codex as
  `dynamicTools` over an in-process MCP bridge, so a tool behaves identically under both engines.
  Every launch disables Codex's own plugin system, update checks and analytics; its state lives in
  a dedicated `codex-home` (`CAROLINE_CODEX_HOME`).
* The installer provisions a pinned, sha256-verified `codex-app-server.exe` under `runtime\codex`
  (`CAROLINE_CODEX_PATH`). If it is missing, only the OpenAI source is unavailable.

## The model ladder

Cheap first, strong only when needed. The same idea exists three times:

* **Claude tab** (`claude_ladder.py`, `chat_session_mixins/ladder.py`): rungs
  `("haiku", "sonnet", "opus")`. A turn starts on Haiku, or higher when the start-rung advice
  (`rung_advice.py`, microagent `turn.rung.advice`) says the matter needs it. When the outside
  verifier finds the attempt did not finish the task, the *same long-lived session* continues with
  the next rung (so it sees everything the previous rung did). `ladder_enabled()` is false under a
  manual model override or `CAROLINE_NO_LADDER`.
* **SW cascade** (`small_model_engine.py`): tiers nano → mini → Sonnet → Opus, with per-tier
  reasoning effort. Escalation is by the model's own sentinel (`[[NEED_ESCALATION]]`), by a
  mechanical loop check (the same `(tool, args)` repeated `REPEATED_CALL_LIMIT` times raises
  `NeedsEscalation`; a softer model-based repeat check runs after `SOFT_LOOP_CHECK_MIN_CALLS`), or
  by the verifier. De-escalation (`[[CAN_DEESCALATE]]`, at most 2) and same-tier retry
  (`[[RETRY_SAME_TIER]]`) exist too. Hard limits: a stall timeout of 120 s per step and 30 min per
  turn. Funds exhaustion waits and retries (60 s interval, up to 30 min) rather than failing.
* **Microagents** (`microagent_client.py`): `nano`, then `nano` again told what failed, then `mini`,
  escalating only when validation of the answer fails.
* **Codex** has a reasoning-effort step chosen before each yield (`_codex_effort_before_yield`).

Stakes-aware behaviour and tier statistics (`sw_tier_stats.py`, microagent `sw.tier.advice`) are
described in `STAKES_AWARE_LADDER_PLAN.md` and `SW_TOOL_ADVISOR_AND_TIER_STATS_PLAN.md`.

## The SW cascade turn

Order of a turn in `small_model_engine.py` (read the code for the exact conditions):

1. The request is formalised by a microagent (`sw_request_spec.py`: who asks, what for, what
   exactly, by when) so models and verifier work from a task, not from raw words.
2. History for the tab comes from `sw_history.py` (`workspace/sw-history/<session>/`: one file per
   tool call plus a rolling summary by `sw.history.compact`), because the CLI's trajectory replay
   does not exist for this engine.
3. The tier runs a tool-calling loop against a `ToolRegistry` built from the plugin roster
   (`build_tool_registry`), plus plan/stage tools (parallel stages with concurrency 4), web search
   (`WEB_SEARCH_ENGINE = "exa"`) and the operations tools.
4. A verifier decides whether the task is done (`_build_verification_prompt`; evidence, grounding,
   action-scope, account-consistency and effect checks are the `sw_*` microagents).
5. `sw_result_writer.py` turns the model's answer into what a person reads, then `reply.review`
   and translation run, as on every engine.
6. Model calls are charged to the user's wallet (`model_billing.py`). `MODEL_CALLS_VIA_SQUIRRELWISDOM`
   is currently `False`, i.e. Caroline's own balance check applies rather than the service's
   billed route (see the module docstring for the history).

`camerlengo` (the shipped server-side helper stubs, resolved by `_resolve_camerlengo_dir`) provides
`AI.resolve_agentic()`; a dev checkout may resolve a sibling `reforce` repository instead, or
`CAROLINE_CAMERLENGO_PATH`.

## Tool groups (what the model sees)

Registering ~270 tools of ~45 plugins on every request cost about 35 thousand tokens. Since
2026-10-26 `tool_groups.py` replaces that with one line per plugin:

```
list_groups()              -> the groups, one line each (also in this tool's description)
list_tools(group)          -> name + short description of each tool in a group
describe_tool(name)        -> full description, JSON schema, the group's usage guidance
call_tool(name, args)      -> runs the tool through the same dispatch() as a direct call
```

* A plugin with `always_loaded=True` stays a normal, directly callable MCP server. Currently the
  memory, skills, dialogue and Akinakes-bus plugins use it.
* `build_mcp_servers()` registers the `caroline-tools` group server plus `caroline-operations`
  (`check_operation_status`, `stop_operation`, `get_tool_instructions`, `describe_own_backend`).
* The SW cascade uses the same text builders; the Claude and Codex engines get the servers from
  `plugins/loader.py`.
* `CAROLINE_FLAT_TOOLS=1` restores the old flat registration (useful to bisect a tool problem).
* Because the model now calls `call_tool`, code that watches tool *names* in the stream
  (memory nudge, watchdog's pending calls, narration facts) must unwrap the inner name; this is a
  known risk area noted in the plans.

## System prompt assembly

The appended system prompt of the Claude and Codex engines is built from:
`persona.persona_system_prompt_append()`, `policies.ALWAYS_ON_INSTRUCTIONS` (the must-never-be-missed
rules, kept short on purpose), the owner's quick profile (`owner_profile.py`), standing rules
(`instruction_memory.py`), the topic summary of the last day and week (`memory_topics.py`), the
working-memory block, the skills catalog (`skills.py`) and a continuity pointer. Sub-agents do not
inherit it, which is why `agent_definitions.py` repeats the essential rules. Keep this block small:
a command line over ~32K characters broke every tab once on Windows (`CreateProcess` limit), which
is why the full owner profile lives in Notes and only a few facts are inlined.

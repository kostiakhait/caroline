"""Microagent framework.

A microagent is a function with one narrow job that is solved by calling an
LLM, usually a small model. This module is the common runtime every
microagent goes through, so that all of them share one interface (JSON in,
JSON out, standard context) and one set of guarantees.

The module has no server dependencies and is kept as an identical copy on the
server (reforce) and on the client (Caroline). The function that actually
calls a model is passed in from outside:

    llm_call(model_class, prompt, timeout_s, reasoning) -> {"text": str, "usage": {...}}

An agent that shows the model pictures (`Agent.images`) is called with one
more argument, the pictures as data URLs: llm_call(..., reasoning, images).

`model_class` is MODEL_NANO or MODEL_MINI; the caller maps it to a real
model. `reasoning` is how hard the model should think on this rung
("minimal", "low", ...), or None for the provider's default. `usage` may carry model, prompt_tokens, completion_tokens and cost
(USD). The function raises NoFundsUser / NoFundsUpstream when a call is
refused for lack of money, and any other exception for any other failure.

Two kinds of agent:
  - an LLM agent declares `build_prompt`; the runtime walks the ladder
    (LADDER) and sanitizes every reply: JSON, then schema, then the agent's
    own `verify`;
  - a composite agent declares `execute`; it is plain code that calls other
    agents through its Context and combines their results.

Guarantees (see docs/MICROAGENTS_PLAN.md, section 3):
  - every agent runs in its own thread; an exception inside one becomes a
    result with status "error" and never reaches another agent;
  - a model call has a hard timeout and an agent has a deadline; an agent
    that overruns is abandoned and reported as failed;
  - the number of agents working at the same time is bounded; an agent that
    is only waiting for the agents it called does not occupy a slot;
  - the budget is shared by the whole chain of calls, a nested agent gets
    what is left and not a fresh allowance;
  - the cost of every model call, in whatever thread it was made, is summed
    into the result and into the ledger of the context.

Partial results: a context may carry `emit`, a function that takes a dict.
An agent that has part of its answer ready before it is finished may hand it
to `context.emit` (when it is set) as soon as it has it; the caller decides
what to do with it, for example pass it on to a client that is waiting. The
agents an agent calls inherit its `emit`, and an agent may replace its own
`context.emit` before calling others to receive -- and relabel, filter or
hold back -- what they emit. `emit` may be called from any thread.

Nothing here returns numeric scores: models rank badly in numbers, so agents
answer in qualitative terms, for example an ordered list of labels.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

MODULE_VERSION = "1.0.0"

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_ERROR = "error"
STATUS_NO_FUNDS_USER = "no_funds_user"
STATUS_NO_FUNDS_UPSTREAM = "no_funds_upstream"

ERR_UNKNOWN_AGENT = "unknown_agent"
ERR_BAD_INPUT = "bad_input"
ERR_BAD_OUTPUT = "bad_output"
ERR_LADDER_EXHAUSTED = "ladder_exhausted"
ERR_BUDGET_EXHAUSTED = "budget_exhausted"
ERR_TIMEOUT = "timeout"
ERR_EXCEPTION = "exception"

MODEL_NANO = "nano"
MODEL_MINI = "mini"
# Per explicit instruction (2026-10-12): a quality-sensitive agent (the literary
# editor's own patch stage) needs stronger models than the default ladder ever
# reaches -- these name the SAME two tiers Config.py already keeps for this
# purpose (AI_MODEL_ALTERNATE, AI_MODEL_LARGE), not a new concept of "large" or
# "sonnet" belonging to this framework specifically. See Agent.ladder below.
MODEL_ALTERNATE = "alternate"
MODEL_LARGE = "large"


@dataclass(frozen=True)
class Rung:
    """One step of the ladder: which class of model to call, whether the
    prompt carries the previous reply and the reason it was rejected, and how
    hard the model should reason (None: the provider's default)."""

    model_class: str
    with_rejection_reason: bool
    reasoning: Optional[str] = None


REASONING_MINIMAL = "minimal"
REASONING_LOW = "low"

# The ladder is the same for every agent: nano, nano again with the reason the
# first reply was rejected, then mini at the provider's default reasoning. The
# first reply that passes sanitizing ends the run. The ladder escalates only on
# a reply that fails sanitizing; a wrong answer in valid form passes on rung 1,
# which is why rung 1 is not run at "minimal".
LADDER: Tuple[Rung, ...] = (
    Rung(MODEL_NANO, False, REASONING_LOW),
    Rung(MODEL_NANO, True, REASONING_LOW),
    Rung(MODEL_MINI, True, None),
)

DEFAULT_CALL_TIMEOUT_S = 60.0
DEFAULT_AGENT_DEADLINE_S = 180.0
DEFAULT_MAX_ACTIVE_AGENTS = 16
DEFAULT_BUDGET_CALLS = 64
DEFAULT_BUDGET_DEADLINE_S = 300.0

LABEL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# How much of a rejected reply is quoted back to the model on the next rung.
MAX_QUOTED_REPLY_CHARS = 4000

OUTPUT_FORMAT_TEMPLATE = (
    "Reply with ONE JSON value and nothing else: no markdown, no code fence, no commentary before or after it. "
    "It must match this JSON Schema:\n{schema}"
)

REJECTED_REPLY_TEMPLATE = (
    "{prompt}\n\n"
    "Your previous reply to this exact task was REJECTED.\n"
    "Previous reply:\n---\n{reply}\n---\n"
    "Reason it was rejected: {reason}\n"
    "Reply again and fix exactly that."
)

FAILED_ATTEMPT_TEMPLATE = (
    "{prompt}\n\n"
    "A previous attempt at this exact task produced no usable reply: {reason}\n"
    "Reply now, following the output format exactly."
)


class NoFundsUser(Exception):
    """The user's own wallet cannot pay for this call."""


class NoFundsUpstream(Exception):
    """The upstream model provider refused the call for lack of funds."""


class AgentStop(Exception):
    """Raised by a composite agent to end its run with a chosen status."""

    def __init__(self, status: str, code: str = "", message: str = "", output: Any = None):
        super().__init__(message or code or status)
        self.status = status
        self.code = code
        self.message = message
        self.output = output


class _Abandoned(Exception):
    """The agent overran its deadline and nobody is waiting for it any more."""


# --- JSON and schema -----------------------------------------------------------

def extract_json(text: Any) -> Tuple[Any, Optional[str]]:
    """Returns (value, None), or (None, reason) when no JSON value can be read
    from the reply. Tolerates a code fence or stray text around the value."""
    if not isinstance(text, str) or not text.strip():
        return None, "the reply is empty"
    stripped = text.strip()
    try:
        return json.loads(stripped), None
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for start, char in enumerate(stripped):
        if char not in "{[":
            continue
        try:
            value, _end = decoder.raw_decode(stripped[start:])
        except ValueError:
            continue
        return value, None
    return None, "the reply is not valid JSON"


_TYPE_CHECKS: Dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(value: Any, schema: Optional[dict], path: str = "$") -> Optional[str]:
    """Checks `value` against a small subset of JSON Schema: type, properties,
    required, additionalProperties, items, enum, minItems, maxItems,
    minLength. Returns the first problem as text, or None when it fits."""
    if not schema:
        return None
    expected = schema.get("type")
    if expected is not None:
        names = expected if isinstance(expected, list) else [expected]
        if not any(_TYPE_CHECKS.get(name, lambda _v: False)(value) for name in names):
            return f"{path} must be of type {' or '.join(names)}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} must be one of {json.dumps(schema['enum'], ensure_ascii=False)}"
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        return f"{path} must be at least {schema['minLength']} characters long"
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                return f'{path} is missing the required field "{name}"'
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    return f'{path} has an unexpected field "{name}"'
        for name, sub_schema in properties.items():
            if name in value:
                problem = validate(value[name], sub_schema, f"{path}.{name}")
                if problem:
                    return problem
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            return f"{path} must have at least {schema['minItems']} item(s)"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{path} must have at most {schema['maxItems']} item(s)"
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                problem = validate(item, item_schema, f"{path}[{index}]")
                if problem:
                    return problem
    return None


# Confirmed live (2026-10-12): a reply can be well-formed JSON matching its
# schema and still be contaminated -- a long-generation task's own string
# field ended with the model echoing back the literal tail of
# OUTPUT_FORMAT_TEMPLATE ("Reply with ONE JSON value and nothing else: no
# markdown, no code fence..."), which passes schema validation untouched
# (it is still a perfectly good string) and leaked straight through to the
# user with no way to tell it apart from the real answer. The first
# sentence of the template is distinctive enough on its own to use as a
# signature; derived from the template itself so a future wording change
# here is automatically covered, not a second copy to keep in sync by hand.
_OUTPUT_FORMAT_LEAK_SIGNATURE = OUTPUT_FORMAT_TEMPLATE.split("\n", 1)[0]


def find_prompt_leak(value: Any, path: str = "$") -> Optional[str]:
    """Recurses into every string field of an already-schema-valid reply,
    looking for leftover output-format instructions the model echoed back
    instead of just answering. Returns the reason to reject the reply (same
    shape as validate()'s own return), or None when the reply is clean."""
    if isinstance(value, str) and _OUTPUT_FORMAT_LEAK_SIGNATURE in value:
        return (
            f'{path} still contains leftover output-format instructions ("{_OUTPUT_FORMAT_LEAK_SIGNATURE[:60]}...") '
            "-- answer the task only, do not repeat or quote these instructions in your reply"
        )
    if isinstance(value, dict):
        for key, item in value.items():
            problem = find_prompt_leak(item, f"{path}.{key}")
            if problem:
                return problem
    if isinstance(value, list):
        for index, item in enumerate(value):
            problem = find_prompt_leak(item, f"{path}[{index}]")
            if problem:
                return problem
    return None


# --- labels and ranking --------------------------------------------------------

def assign_labels(items: Sequence[Any], alphabet: str = LABEL_ALPHABET) -> List[Tuple[str, Any]]:
    """Pairs every item with a short label for one model call. The model is
    shown labels, never the real names of what stands behind them."""
    if len(items) > len(alphabet):
        raise ValueError(f"{len(items)} items do not fit the label alphabet of {len(alphabet)}")
    return [(alphabet[index], item) for index, item in enumerate(items)]


def check_ranking(offered: Sequence[str], answer: Any) -> Optional[str]:
    """A ranking is valid when it holds exactly the offered labels, each once.
    Returns the reason it is not, or None."""
    if not isinstance(answer, list):
        return "the ranking must be a list of labels"
    not_text = [item for item in answer if not isinstance(item, str)]
    if not_text:
        return "every item of the ranking must be a label given as text"
    unknown = [label for label in answer if label not in offered]
    if unknown:
        return f"the ranking contains label(s) that were not offered: {', '.join(unknown)}"
    repeated = sorted({label for label in answer if answer.count(label) > 1})
    if repeated:
        return f"the ranking repeats label(s): {', '.join(repeated)}"
    missing = [label for label in offered if label not in answer]
    if missing:
        return f"the ranking leaves out label(s): {', '.join(missing)}"
    return None


# --- budget and ledger ---------------------------------------------------------

class Budget:
    """What one request may spend. Shared by every agent in the chain."""

    def __init__(self, calls: int = DEFAULT_BUDGET_CALLS, deadline_s: float = DEFAULT_BUDGET_DEADLINE_S):
        self._lock = threading.Lock()
        self._calls_left = calls
        self._deadline_at = time.monotonic() + deadline_s

    def take_call(self) -> bool:
        with self._lock:
            if self._calls_left <= 0:
                return False
            self._calls_left -= 1
            return True

    @property
    def calls_left(self) -> int:
        with self._lock:
            return self._calls_left

    def seconds_left(self) -> float:
        return self._deadline_at - time.monotonic()


class _Usage:
    """Sum of model calls. Not thread-safe on its own."""

    def __init__(self) -> None:
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost = 0.0
        self.models: List[str] = []

    def add_call(self, usage: Optional[dict]) -> None:
        self.calls += 1
        usage = usage or {}
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self.cost += float(usage.get("cost") or 0.0)
        model = usage.get("model")
        if model and model not in self.models:
            self.models.append(model)

    def add_summary(self, summary: Optional[dict]) -> None:
        summary = summary or {}
        self.calls += int(summary.get("calls") or 0)
        self.prompt_tokens += int(summary.get("prompt_tokens") or 0)
        self.completion_tokens += int(summary.get("completion_tokens") or 0)
        self.cost += float(summary.get("cost") or 0.0)
        for model in summary.get("models") or []:
            if model not in self.models:
                self.models.append(model)

    def summary(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "tokens": self.prompt_tokens + self.completion_tokens,
            "cost": self.cost,
            "models": list(self.models),
        }


class Ledger:
    """Every model call of one request, from whatever thread it was made.
    Billing reads this: unlike the usage rolled up in a result, it also holds
    calls made by an agent that was abandoned and finished late."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._usage = _Usage()

    def add_call(self, usage: Optional[dict]) -> None:
        with self._lock:
            self._usage.add_call(usage)

    def snapshot(self) -> dict:
        with self._lock:
            return self._usage.summary()


# --- agent, context ------------------------------------------------------------

@dataclass
class Agent:
    """Declaration of a microagent. Exactly one of `build_prompt` (LLM agent)
    and `execute` (composite agent) is set.

    description   one or two lines; listings of agents are built from it.
    build_prompt  (input, context) -> the task text. The output format is
                  appended by the runtime from `output_schema`.
    verify        (input, output) -> the reason the output is unacceptable,
                  or None. Runs after the schema check.
    is_empty      (output) -> True when the output holds nothing useful; the
                  result then has status "empty".
    execute       (input, context) -> output. Calls other agents through
                  context.run / context.run_many.
    images        (input) -> pictures to show the model with the prompt, as
                  data URLs ("data:image/png;base64,..."); LLM agents only.
                  They are never written to the log.
    ladder        overrides the Runtime's own default ladder for this one LLM
                  agent, when set (None: use the Runtime's). Per explicit
                  instruction (2026-10-12): the first agent that genuinely
                  needs this is the literary editor's patch stage, which must
                  reach stronger models (MODEL_ALTERNATE, MODEL_LARGE) than
                  the default ladder's nano/mini ever call -- see that
                  agent's own module for its ladder.
    deadline_s    overrides the Runtime's own default agent deadline for this
                  one agent, when set (None: use the Runtime's). Confirmed
                  live (2026-10-12): the literary editor's own patch agent,
                  on `ladder` above, can genuinely need all three of its rungs
                  against real models -- three sequential real network calls
                  do not reliably fit the default 180s meant for the cheap
                  nano/mini ladder.
    """

    name: str
    version: str
    description: str
    input_schema: Optional[dict] = None
    output_schema: Optional[dict] = None
    build_prompt: Optional[Callable[[Any, "Context"], str]] = None
    verify: Optional[Callable[[Any, Any], Optional[str]]] = None
    is_empty: Optional[Callable[[Any], bool]] = None
    execute: Optional[Callable[[Any, "Context"], Any]] = None
    images: Optional[Callable[[Any], List[str]]] = None
    ladder: Optional[Tuple[Rung, ...]] = None
    deadline_s: Optional[float] = None


class Context:
    """Standard parameters of a call, the same for every agent."""

    def __init__(
        self,
        runtime: "Runtime",
        session: Optional[str],
        turn_id: Optional[str],
        trace_id: str,
        language: Optional[str],
        budget: Budget,
        ledger: Ledger,
        job: Optional["_Job"] = None,
        emit: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.session = session
        self.turn_id = turn_id
        self.trace_id = trace_id
        self.language = language
        self.budget = budget
        self.ledger = ledger
        self.emit = emit  # partial results, see the module docstring
        self._runtime = runtime
        self._job = job
        self._children: List[dict] = []

    def run(self, agent: str, input: Any) -> dict:
        return self._runtime._run_children(self, [(agent, input)])[0]

    def run_many(self, calls: Sequence[Tuple[str, Any]]) -> List[dict]:
        """Runs several agents at the same time; results come back in the
        order of `calls`."""
        return self._runtime._run_children(self, list(calls))


class _Job:
    def __init__(self, agent: Agent, input: Any, context: Context, deadline_at: float) -> None:
        self.agent = agent
        self.input = input
        self.context = context
        self.deadline_at = deadline_at
        self.started_at = time.monotonic()
        self.done = threading.Event()
        self.result: Optional[dict] = None
        self.lock = threading.Lock()
        self.permit_held = False
        self.abandoned = False


def first_no_funds(results: Sequence[dict]) -> Optional[dict]:
    """The first result that failed for lack of money, if any."""
    for result in results:
        if result.get("status") in (STATUS_NO_FUNDS_USER, STATUS_NO_FUNDS_UPSTREAM):
            return result
    return None


# --- runtime -------------------------------------------------------------------

class Runtime:
    def __init__(
        self,
        llm_call: Callable[[str, str, float, Optional[str]], dict],
        ladder: Sequence[Rung] = LADDER,
        call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
        agent_deadline_s: float = DEFAULT_AGENT_DEADLINE_S,
        max_active_agents: int = DEFAULT_MAX_ACTIVE_AGENTS,
        log: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._llm_call = llm_call
        self._ladder = tuple(ladder)
        self._call_timeout_s = call_timeout_s
        self._agent_deadline_s = agent_deadline_s
        self._permits = threading.BoundedSemaphore(max_active_agents)
        self._log_sink = log
        self._agents: Dict[str, Agent] = {}

    # -- registry --

    def register(self, agent: Agent) -> None:
        if agent.name in self._agents:
            raise ValueError(f'microagent "{agent.name}" is already registered')
        if (agent.build_prompt is None) == (agent.execute is None):
            raise ValueError(f'microagent "{agent.name}" must define exactly one of build_prompt and execute')
        self._agents[agent.name] = agent

    def cards(self, prefix: str = "") -> List[dict]:
        """Name and short description of every registered agent whose name
        starts with `prefix`."""
        return [
            {"name": agent.name, "description": agent.description}
            for agent in self._agents.values()
            if agent.name.startswith(prefix)
        ]

    # -- entry points --

    def new_context(
        self,
        session: Optional[str] = None,
        turn_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        language: Optional[str] = None,
        budget: Optional[Budget] = None,
        emit: Optional[Callable[[dict], None]] = None,
    ) -> Context:
        return Context(self, session, turn_id, trace_id or uuid.uuid4().hex, language, budget or Budget(), Ledger(), emit=emit)

    def run(self, agent: str, input: Any, context: Optional[Context] = None) -> dict:
        return self._run_children(context or self.new_context(), [(agent, input)])[0]

    def run_many(self, calls: Sequence[Tuple[str, Any]], context: Optional[Context] = None) -> List[dict]:
        return self._run_children(context or self.new_context(), list(calls))

    # -- running agents --

    def _run_children(self, parent: Context, calls: List[Tuple[str, Any]]) -> List[dict]:
        parent_job = parent._job
        if parent_job is not None and parent_job.abandoned:
            raise _Abandoned()
        pending: List[Any] = []
        for name, input in calls:
            pending.append(self._spawn(parent, name, input))
        # While it waits, the calling agent does not occupy a slot: otherwise a
        # full set of waiting parents would leave no slot for their children.
        if parent_job is not None:
            self._release_permit(parent_job)
        try:
            results = [item if isinstance(item, dict) else self._collect(item) for item in pending]
        finally:
            if parent_job is not None and not self._acquire_permit(parent_job):
                raise _Abandoned()
        parent._children.extend(results)
        return results

    def _spawn(self, parent: Context, name: str, input: Any) -> Any:
        """Starts the agent in its own thread and returns the job, or returns
        an error result straight away when it cannot be started at all."""
        agent = self._agents.get(name)
        if agent is None:
            return _result(STATUS_ERROR, name, "", error=(ERR_UNKNOWN_AGENT, f'no microagent named "{name}"'))
        seconds_left = parent.budget.seconds_left()
        if seconds_left <= 0:
            return _result(STATUS_ERROR, name, agent.version, error=(ERR_BUDGET_EXHAUSTED, "the request deadline has passed"))
        # A composite agent only orchestrates other agents, each with its own
        # deadline, so it is bounded by the request budget alone; giving it the
        # deadline of a single agent abandons it while its children still work.
        own_limit = seconds_left if agent.execute is not None else min(agent.deadline_s or self._agent_deadline_s, seconds_left)
        deadline_at = time.monotonic() + own_limit
        context = Context(self, parent.session, parent.turn_id, parent.trace_id, parent.language, parent.budget, parent.ledger,
                          emit=parent.emit)
        job = _Job(agent, input, context, deadline_at)
        context._job = job
        threading.Thread(target=self._body, args=(job,), name=f"microagent:{name}", daemon=True).start()
        return job

    def _collect(self, job: _Job) -> dict:
        if not job.done.wait(max(0.0, job.deadline_at - time.monotonic())):
            with job.lock:
                timed_out = not job.done.is_set()
                if timed_out:
                    job.abandoned = True
                    held, job.permit_held = job.permit_held, False
            if timed_out:
                if held:
                    self._permits.release()
                self._log({"event": "agent_abandoned", "agent": job.agent.name, "trace_id": job.context.trace_id})
                return _result(
                    STATUS_ERROR, job.agent.name, job.agent.version,
                    error=(ERR_TIMEOUT, "the agent did not finish before its deadline"),
                    ms=_ms_since(job.started_at),
                )
        assert job.result is not None
        return job.result

    def _body(self, job: _Job) -> None:
        """Thread of one agent. Nothing raised in here leaves the thread."""
        try:
            if not self._acquire_permit(job):
                result = self._finish(job, STATUS_ERROR, error=(ERR_TIMEOUT, "no free agent slot before the deadline"))
            else:
                logged_input = job.input if job.agent.images is None else "(input with pictures, not logged)"
                self._log({"event": "agent_start", "agent": job.agent.name, "trace_id": job.context.trace_id, "input": logged_input})
                result = self._execute(job)
        except _Abandoned:
            result = self._finish(job, STATUS_ERROR, error=(ERR_TIMEOUT, "the agent was abandoned"))
        except BaseException as exc:  # noqa: BLE001 -- the boundary: a crash here must stay here
            result = self._finish(job, STATUS_ERROR, error=(ERR_EXCEPTION, f"{type(exc).__name__}: {exc}"))
        finally:
            self._release_permit(job)
        self._log({
            "event": "agent_end", "agent": job.agent.name, "trace_id": job.context.trace_id,
            "status": result["status"], "output": result["output"], "error": result["error"], "usage": result["usage"],
        })
        with job.lock:
            if not job.abandoned:
                job.result = result
                job.done.set()

    def _execute(self, job: _Job) -> dict:
        agent = job.agent
        problem = validate(job.input, agent.input_schema)
        if problem:
            return self._finish(job, STATUS_ERROR, error=(ERR_BAD_INPUT, problem))
        if agent.execute is not None:
            return self._run_composite(job)
        return self._run_ladder(job)

    def _run_composite(self, job: _Job) -> dict:
        agent = job.agent
        try:
            output = agent.execute(job.input, job.context)  # type: ignore[misc]
        except AgentStop as stop:
            error = (stop.code, stop.message) if stop.status == STATUS_ERROR or stop.code or stop.message else None
            return self._finish(job, stop.status, output=stop.output, error=error)
        problem = validate(output, agent.output_schema)
        if problem:
            return self._finish(job, STATUS_ERROR, error=(ERR_BAD_OUTPUT, problem))
        return self._finish(job, self._status_for(agent, output), output=output)

    def _run_ladder(self, job: _Job) -> dict:
        agent, context = job.agent, job.context
        task = agent.build_prompt(job.input, context)  # type: ignore[misc]
        images = agent.images(job.input) if agent.images is not None else None
        base_prompt = task + "\n\n" + OUTPUT_FORMAT_TEMPLATE.format(schema=json.dumps(agent.output_schema or {}, ensure_ascii=False))
        own = _Usage()
        attempts: List[dict] = []
        last_reply: Optional[str] = None
        last_reason: Optional[str] = None
        for number, rung in enumerate(agent.ladder or self._ladder, start=1):
            if job.abandoned:
                raise _Abandoned()
            if not context.budget.take_call():
                return self._finish(job, STATUS_ERROR, error=(ERR_BUDGET_EXHAUSTED, "no model calls left in the budget"), own=own, attempts=attempts)
            prompt = base_prompt
            if rung.with_rejection_reason and last_reason:
                if last_reply is None:
                    prompt = FAILED_ATTEMPT_TEMPLATE.format(prompt=base_prompt, reason=last_reason)
                else:
                    prompt = REJECTED_REPLY_TEMPLATE.format(prompt=base_prompt, reply=last_reply[:MAX_QUOTED_REPLY_CHARS], reason=last_reason)
            attempt: dict = {"rung": number, "model_class": rung.model_class, "reasoning": rung.reasoning}
            attempts.append(attempt)
            timeout_s = max(0.0, min(self._call_timeout_s, job.deadline_at - time.monotonic(), context.budget.seconds_left()))
            self._log({"event": "model_call", "agent": agent.name, "trace_id": context.trace_id, "rung": number, "model_class": rung.model_class, "prompt": prompt})
            try:
                if images:
                    reply = self._llm_call(rung.model_class, prompt, timeout_s, rung.reasoning, images)
                else:
                    reply = self._llm_call(rung.model_class, prompt, timeout_s, rung.reasoning)
            except NoFundsUser as exc:
                own.add_call(None)
                context.ledger.add_call(None)
                attempt["error"] = str(exc)
                return self._finish(job, STATUS_NO_FUNDS_USER, error=(STATUS_NO_FUNDS_USER, str(exc)), own=own, attempts=attempts)
            except NoFundsUpstream as exc:
                own.add_call(None)
                context.ledger.add_call(None)
                attempt["error"] = str(exc)
                return self._finish(job, STATUS_NO_FUNDS_UPSTREAM, error=(STATUS_NO_FUNDS_UPSTREAM, str(exc)), own=own, attempts=attempts)
            except Exception as exc:  # noqa: BLE001 -- a failed call is a failed rung, the ladder goes on
                own.add_call(None)
                context.ledger.add_call(None)
                last_reply, last_reason = None, f"the model call failed ({type(exc).__name__}: {exc})"
                attempt["error"] = last_reason
                self._log({"event": "model_failed", "agent": agent.name, "trace_id": context.trace_id, "rung": number, "error": last_reason})
                continue
            text = reply.get("text") if isinstance(reply, dict) else None
            usage = reply.get("usage") if isinstance(reply, dict) else None
            own.add_call(usage)
            context.ledger.add_call(usage)
            attempt["model"] = (usage or {}).get("model")
            self._log({"event": "model_reply", "agent": agent.name, "trace_id": context.trace_id, "rung": number, "text": text, "usage": usage})
            output, reason = extract_json(text)
            if reason is None:
                reason = validate(output, agent.output_schema)
            if reason is None:
                reason = find_prompt_leak(output)
            if reason is None and agent.verify is not None:
                reason = agent.verify(job.input, output)
            if reason is None:
                return self._finish(job, self._status_for(agent, output), output=output, own=own, attempts=attempts, rung=number)
            attempt["rejected"] = reason
            last_reply, last_reason = text if isinstance(text, str) else "", reason
            self._log({"event": "reply_rejected", "agent": agent.name, "trace_id": context.trace_id, "rung": number, "reason": reason})
        return self._finish(job, STATUS_ERROR, error=(ERR_LADDER_EXHAUSTED, last_reason or "every rung of the ladder failed"), own=own, attempts=attempts)

    @staticmethod
    def _status_for(agent: Agent, output: Any) -> str:
        return STATUS_EMPTY if agent.is_empty is not None and agent.is_empty(output) else STATUS_OK

    def _finish(
        self,
        job: _Job,
        status: str,
        output: Any = None,
        error: Optional[Tuple[str, str]] = None,
        own: Optional[_Usage] = None,
        attempts: Optional[List[dict]] = None,
        rung: Optional[int] = None,
    ) -> dict:
        total = _Usage()
        if own is not None:
            total.add_summary(own.summary())
        for child in job.context._children:
            total.add_summary(child.get("usage"))
        return _result(
            status, job.agent.name, job.agent.version, output=output, error=error, usage=total.summary(),
            rung=rung, ms=_ms_since(job.started_at), attempts=attempts,
            children=[child.get("trace") for child in job.context._children],
        )

    # -- slots --

    def _acquire_permit(self, job: _Job) -> bool:
        if not self._permits.acquire(timeout=max(0.0, job.deadline_at - time.monotonic())):
            return False
        with job.lock:
            if job.abandoned:
                abandoned = True
            else:
                abandoned = False
                job.permit_held = True
        if abandoned:
            self._permits.release()
            return False
        return True

    def _release_permit(self, job: _Job) -> None:
        with job.lock:
            held, job.permit_held = job.permit_held, False
        if held:
            self._permits.release()

    # -- logging --

    def _log(self, event: dict) -> None:
        if self._log_sink is None:
            return
        try:
            self._log_sink(event)
        except Exception:  # noqa: BLE001 -- a broken log sink must not break the agent
            pass


def _ms_since(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def _result(
    status: str,
    agent: str,
    version: str,
    output: Any = None,
    error: Optional[Tuple[str, str]] = None,
    usage: Optional[dict] = None,
    rung: Optional[int] = None,
    ms: int = 0,
    attempts: Optional[List[dict]] = None,
    children: Optional[List[Any]] = None,
) -> dict:
    """The standard result of every agent run."""
    summary = dict(usage or _Usage().summary())
    summary["rung"] = rung
    summary["ms"] = ms
    return {
        "status": status,
        "output": output,
        "usage": summary,
        "trace": {
            "agent": agent,
            "version": version,
            "status": status,
            "rung": rung,
            "ms": ms,
            "error": error[0] if error else None,
            "attempts": attempts or [],
            "children": children or [],
        },
        "error": {"code": error[0], "message": error[1]} if error else None,
    }

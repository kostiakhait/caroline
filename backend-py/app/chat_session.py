"""One ChatSession per open tab.

Split 2026-10-17 into app/chat_session_shared.py (imports, module-level
constants/helpers, ToolOutcome) and app/chat_session_mixins/* (one file per
subsystem: engine lifecycle, turn submission, small-model turn, stop
control, permission guard, text/voice narration, memory replay,
status/conn state, retry/reconnect, dialogue/transcript, reply-watch,
watchdog/hang-detection, compaction). This file keeps only __init__ (which
sets up state every subsystem shares) and the class declaration itself.
See chat_session_shared.py's own docstring for the original design notes
this split carried forward unchanged.
"""

from __future__ import annotations

from app.chat_session_shared import *  # noqa: F401,F403 -- see that module's own header
from app.chat_session_mixins import (
    EngineLifecycleMixin,
    TurnSubmissionMixin,
    SmallModelTurnMixin,
    StopControlMixin,
    PermissionGuardMixin,
    TextNarrationMixin,
    VoiceNarrationMixin,
    MemoryReplayMixin,
    StatusConnMixin,
    RetryReconnectMixin,
    DialogueTranscriptMixin,
    ReplyWatchMixin,
    WatchdogMixin,
    CompactionMixin,
    # edition:cut claude model ladder
    LadderMixin,
    # edition:end
    DeliveryReportMixin,
    CompactionRecoveryMixin,
)


class ChatSession(
    EngineLifecycleMixin,
    TurnSubmissionMixin,
    SmallModelTurnMixin,
    StopControlMixin,
    PermissionGuardMixin,
    TextNarrationMixin,
    VoiceNarrationMixin,
    MemoryReplayMixin,
    StatusConnMixin,
    RetryReconnectMixin,
    DialogueTranscriptMixin,
    ReplyWatchMixin,
    WatchdogMixin,
    CompactionMixin,
    # edition:cut claude model ladder
    LadderMixin,
    # edition:end
    DeliveryReportMixin,
    CompactionRecoveryMixin,
):
    def __init__(
        self, tab_id: str, workspace_dir: str, send: SendFn, *,
        headless: bool = False, approval_bridge_tab_id: str | None = None,
    ) -> None:
        self.tab_id = tab_id
        self.workspace_dir = workspace_dir
        self.send = self._dr_wrap_send(send)  # records what really reaches the window (delivery_report.py)
        # Per explicit instruction (2026-10-13): a session with no chat window
        # of its own -- the Ratatosk owner-DM channel, and every tab's own
        # pseudo-session (see main.py's get_or_create_ratatosk_session/
        # app/pseudo_tab.py's get_or_create_pseudo_session_sync). Gates the
        # progress-narrator entirely
        # (_check_progress_narration's own early-return) -- nobody is watching
        # a headless session live, so there is nothing for it to narrate TO,
        # and the paid generate_progress_comment call would be pure waste --
        # and skips the wire-translate step inside _translate_wire_visible_text
        # (current_language_name(tab_id) is only ever written by a REAL user's
        # own submit(), which a headless session never has, so translating
        # inside it would permanently stick at the English fallback; the one
        # real translation happens once, at relay time, under the RECEIVING
        # tab's own real language -- see deliver_relayed_reply).
        self.headless = headless
        # Per explicit instruction (2026-10-13): the approval/prohibition
        # "bridge" -- the tab id (almost always the primary tab) whose recent
        # real user messages this session's own permission-guard check should
        # also see, via session_context.get_recent_real_user_directives. A
        # headless background session has none of its own real user messages
        # (nothing ever submits to it with is_real_user=True), so without this
        # its own _check_permission_guard would have no way to know the user
        # already approved something in their real conversation elsewhere.
        # None for every ordinary user-facing tab (no bridging -- a real tab
        # already sees its own real dialogue directly).
        self.approval_bridge_tab_id = approval_bridge_tab_id
        # A session whose proactive turns are tasks to do (2026-10-31): an akinakes chain, where a bus task arrives
        # through inject_proactive(). Its turns are formalized like a real user's request, so the task text reaches
        # the checks of changing calls (sw.action.scope, sw.content.grounding) and the tier advice. Without it the
        # writer's sections went out with invented results unchecked (second real run). Off for every other session:
        # a reminder or a messenger note is not the user's task.
        self.proactive_turns_are_tasks = False
        # Per explicit instruction (2026-10-13): pure in-process, time-pruned
        # memory of this tab's own real user messages -- (epoch_ms, text)
        # pairs, appended once per genuinely-new real submit() (see that
        # method's own is_real_user branch), read (and the list itself pruned)
        # by recent_real_user_directives_text. Deliberately NEVER touches disk
        # or the session transcript file -- unlike PERMISSION_GUARD_DIALOGUE_
        # WINDOW's line-count cap (confirmed live, 2026-10-13: a real approval
        # scrolled out of that 60-line window in well under an hour on a
        # high-traffic tab), this is immune to both traffic volume and context
        # compaction by construction.
        self.recent_real_user_messages: list[tuple[float, str]] = []
        # See _on_memory_upstream_no_funds: the turn to start again once the
        # memory service's provider has money, and the flat timer checking for that.
        self._memory_replay: dict[str, Any] | None = None
        # Set by stop() when it kills an engine that was idle between turns; read by the run loop.
        self._stop_killed_idle_engine = False
        # Set by stop(); the user's next real message carries a note (its own block) that the previous turn was killed.
        self._stop_note_pending = False
        # Set while a forced compaction waits for the save-to-memory turn it asked for.
        self._memory_flushed_for_compaction = False
        self._stop_note_in_next_push = False
        self._memory_replay_timer: asyncio.TimerHandle | None = None
        # See _arm_reply_watch/_reply_watch_tick: the one outstanding
        # reply-expected watch for this tab (email only), and its flat
        # re-arming timer. A later qualifying send replaces whatever watch
        # was already running, same as _memory_replay_timer.
        self._reply_watch: dict[str, Any] | None = None
        self._reply_watch_timer: asyncio.TimerHandle | None = None
        memory_turns.set_upstream_no_funds_handler(tab_id, self._on_memory_upstream_no_funds)

        # edition:cut claude-openai engine
        self.client: AgentEngine | None = None
        # edition:end
        #ic| self.client = None
        # Which engine the current/next query() runs on: "openai" only while the
        # tab is switched to OpenAI AND it is actually usable, else "claude".
        self.engine_kind: str = "claude"
        self.engine_switch_pending = False
        self.ended = False
        self.user_stop_requested = False

        # queue / turn plumbing
        self.queue: list[dict[str, Any]] = []
        self._queue_event = asyncio.Event()
        # User messages waiting out USER_MESSAGE_MERGE_WINDOW_S (see submit_or_try_small_model).
        self._held_user_messages: list[tuple[str, list[Any], bool]] = []
        self._held_user_flush: asyncio.TimerHandle | None = None
        # reply.review calls started before their text is known to be final -- see
        # _review_ahead. (text, task) -> the call.
        self._reviews_ahead: dict[tuple[str, str], asyncio.Task[str | None]] = {}
        # The fast lane already running on what is held (see _start_fast_lane_ahead):
        # what it was asked, and the task answering it.
        self._fast_lane_ahead: tuple[tuple[Any, ...], asyncio.Task[Any]] | None = None
        self.turn_is_voice = False
        self.classifier_refusal_retry_count = 0
        # Backing field for the turn_pending property (defined below,
        # outside __init__) -- set directly here, not via self.turn_pending
        # = False, so construction doesn't trigger a premature status
        # publish before start() has even run.
        self._turn_pending = False
        self.turn_pending_since: float | None = None
        self._last_known_funds_exhausted_reason: str | None = None
        self.pending_user_text: str | None = None
        # Progress remarks already shown to the user (see _send_progress_narration),
        # not yet seen by the model -- delivered with the next submitted message.
        self.pending_interim_notes: list[str] = []
        self.pending_is_real_user: bool = False
        self.pending_attachments: list[Any] = []
        # The current turn's own last real visible assistant text -- see
        # _check_refusal_and_maybe_retry's own docstring. Reset at each
        # fresh real turn's start (submit()), updated as visible text
        # actually reaches the wire, consumed (and cleared) once checked.
        self._turn_last_visible_text: str | None = None
        # Per explicit instruction (2026-10-07): a reminder-driven turn
        # (service or user event, see scheduler_plugin.py's EVENT_TYPES)
        # never gets progress-narrator chatter -- see _check_progress_
        # narration's own gate. Recomputed fresh by submit() ONLY when
        # starting a genuinely NEW turn (mirrors pending_is_real_user's own
        # "never downgrade an already-pending turn" guard) -- deliberately
        # NOT a session-wide latch like the removed silent_turn flag (see
        # inject_proactive's own docstring for why that one was deleted
        # outright rather than patched again): this is recomputed every
        # single new turn from that call's own argument, so it can never
        # leak into or silence an unrelated later turn.
        self.current_turn_suppress_narration: bool = False

        # Per explicit instruction (2026-09-13), after a real incident: a
        # PROACTIVE turn (a scheduled mailbox check, here) can complete
        # with genuinely EMPTY visible content -- confirmed live via the
        # raw session transcript, a real final assistant message with
        # stop_reason="end_turn" and real spent output tokens, but its
        # content array held only an empty thinking block, no text at all
        # (not even a [[NO_UPDATE]] sentinel -- that's a real text block,
        # just client-suppressed, and would count as "visible" here).
        # _fire_post_turn_completion_check() previously only ever fired for
        # REAL user turns (see its own docstring for why proactive turns
        # were excluded -- most proactive completions are LEGITIMATELY
        # silent via NO_UPDATE, and re-nudging every single one would be
        # wasteful). turn_saw_any_visible_text closes exactly the gap
        # between "legitimately silent" and "silently lost real content":
        # reset per-turn in submit(), set True the moment any assistant
        # wire message carries real text (NO_UPDATE included) -- see the
        # wire-send site's own comment.
        self.turn_saw_any_visible_text: bool = False
        # One-shot guard so the completion-check's OWN reply (itself a
        # proactive turn, submitted via inject_proactive) can't chain into
        # firing this same check again if IT also happens to come back
        # empty -- fires at most once per originating turn, never an
        # infinite loop of self-nudges.
        self._awaiting_post_turn_check_reply: bool = False
        # Loop break state for _fire_post_turn_completion_check -- see POST_TURN_CHECK_MAX_ROUNDS.
        self._post_turn_check_rounds: int = 0
        self._post_turn_checks_halted: bool = False

        # Small-model primary path (2026-09-12, see small_model_engine.py's
        # own module docstring) -- an alternative to the SDK path above for
        # simple, tool-using turns, tried first via
        # submit_or_try_small_model() when eligible. small_model_active is
        # the ONLY thing that distinguishes "a turn is running" here from a
        # normal SDK turn (both set turn_pending True) -- every other piece
        # of code that branches on which engine is live checks this flag.
        self.small_model_active: bool = False
        # The live, in-memory exchange for the turn CURRENTLY running
        # through the small model -- set by run_small_model_turn's own
        # on_live_dialogue_update callback. Nothing this path does ever
        # touches the on-disk Claude Code .jsonl transcript (it never talks
        # to the CLI at all), so _gather_recent_dialogue_for_narration must
        # prefer this over its usual disk read while a small-model turn is
        # active, or the narrator would see nothing happening.
        self.small_model_live_dialogue: list[str] | None = None
        # Real activity of the running small-model turn (tiers, tool calls, verification) -- the
        # narrator may only restate these facts. See small_model_engine's on_activity.
        self.small_model_activity: list[str] = []
        # True while the CURRENTLY running small-model turn is a real user turn
        # (False for a proactive/internal one) -- see _route_sw_turn.
        self.small_model_turn_is_real_user: bool = False
        # Real user messages that arrived while a PROACTIVE small-model turn was
        # running (2026-10-03): queued here instead of folded into that turn as a
        # live comment, and started as their OWN fresh turn once it ends -- a real
        # request must never be swallowed by unrelated internal work.
        self.small_model_queued_real_turns: list[tuple[str, list[Any], bool]] = []
        # New user_message text that arrived WHILE a small-model turn was
        # already running -- per explicit instruction, this is live context
        # fed into the SAME turn (via get_new_user_comments, polled once per
        # resolve_agentic() iteration), never a fresh submit() and never an
        # implicit cancellation of the turn in progress.
        self.small_model_pending_comments: list[str] = []
        self._small_model_task: asyncio.Task[None] | None = None
        # The sw engine's own memory across turns (app/sw_history.py): the
        # current turn's tool-call recorder, and the previous turn's
        # background summary/compaction the next turn waits for.
        self._sw_turn_recorder: Any = None
        self._sw_history_job: asyncio.Task[None] | None = None

        # Forced compaction (see FORCED_COMPACTION_HOURLY_MS's own comment).
        # needs_startup_compaction is set True by main.py, once per tab_id
        # per PROCESS lifetime (a module-level set there, mirroring how the
        # startup greeting/pending-turn-crash-recovery already track "once
        # per process, not per reconnect") -- this ChatSession instance
        # itself gets recreated on every WS reconnect, so instance state
        # alone can't carry that scope.
        self.needs_startup_compaction: bool = False
        self.last_forced_compaction_at: float | None = None
        self.size_at_last_forced_compaction: int | None = None
        # Bug fix (2026-09-15), per explicit instruction -- confirmed live
        # via a direct test (see the "гипотеза B" investigation): the
        # on-disk .jsonl transcript is append-only -- a successful /compact
        # cuts real API-context tokens drastically (confirmed: 69422 ->
        # 10158, a real compact_boundary system message) but the FILE only
        # ever grows (never shrinks), so size_at_last_forced_compaction/
        # FORCED_COMPACTION_GROWTH_BYTES_THRESHOLD above were comparing
        # against a baseline that can never reflect what compaction
        # actually accomplished -- the growth trigger was watching the
        # wrong signal entirely. Track real context tokens instead:
        # tokens_at_last_forced_compaction is the authoritative baseline,
        # set from a compact_boundary system message's own
        # compact_metadata.post_tokens (the CLI's own count of what's left
        # right after compacting -- see the SystemMessage handling below);
        # last_known_context_tokens is updated from every REAL (non-fake)
        # ResultMessage's own usage field, the live "how much is actually
        # in context right now" signal Claude Code already reports on
        # every turn. Both None until the first real value arrives --
        # _check_forced_compaction's growth branch simply doesn't fire
        # until then (same graceful-startup shape as last_forced_
        # compaction_at being None), which is fine: "startup" already
        # forces an initial compaction every process start regardless.
        self.tokens_at_last_forced_compaction: int | None = None
        self.last_known_context_tokens: int | None = None
        # Per explicit instruction (2026-09-15): set by _apply_conn_state
        # whenever conn_state leaves "limited" -- see its own comment for
        # the incident (a forced-compaction attempt that itself hit the
        # usage cap left forced_compaction_result_pending stuck forever,
        # silently blocking every future compaction attempt even once the
        # cap reset). Consumed by _check_forced_compaction, which bypasses
        # its normal cooldown for this specific reason -- the point is to
        # retry what the cap just interrupted, not wait for the next
        # regular cycle.
        self.needs_post_limit_compaction_check: bool = False
        # time.monotonic() before which no forced compaction is attempted --
        # set when a forced compaction itself hits the usage cap (see
        # FORCED_COMPACTION_LIMIT_PAUSE_MS / _note_compaction_hit_limit).
        self.forced_compaction_blocked_until: float = 0.0
        # Same "this ResultMessage/whatever precedes it isn't real, don't
        # show it or let it touch turn state" shape as
        # hang_interrupt_result_pending/ignore_next_result_recovery -- see
        # result_is_fake's own computation in the message loop.
        self.forced_compaction_result_pending: bool = False
        # Bug fix (2026-09-18), per explicit instruction after a real
        # incident: a real user message submitted WHILE a forced compaction
        # is still in flight for this tab used to race straight into the
        # live client's input stream -- confirmed live, the CLI's own
        # compact boundary landed BEFORE that message got processed, and
        # the reply that eventually came back claimed the user's own
        # phrase "got cut off" (it genuinely had -- the compacted context
        # didn't include it). submit() now queues here instead of pushing
        # to the client whenever forced_compaction_result_pending is True;
        # _drain_compaction_queue() replays every queued item, in order,
        # through the normal submit() path (so post_turn_completion_check/
        # narration/durability all apply exactly as they would for a live
        # submit -- nothing special-cased) the moment compaction actually
        # finishes.
        self.compaction_queued_turns: list[dict[str, Any]] = []

        # Per explicit instruction (2026-10-15), a real incident: a
        # background pseudo-session relayed raw, untranslated, internal-
        # mechanics chatter straight into the user's visible chat because
        # the SAME upstream OpenRouter balance exhaustion broke BOTH the
        # relay-worth check AND the translate step that would otherwise
        # have caught it -- two independent fail-open safety nets, same
        # root cause, both down at once. Direct instruction afterward: "При
        # недостатке баланса у провайдера ВСЯ деятельность Кэролайн должна
        # ПОЛНОСТЬЮ блокироваться до пополнения баланса" -- every new turn
        # queues here instead of starting, exact same shape as
        # compaction_queued_turns/forced_compaction_result_pending above,
        # while sw_api.get_funds_exhausted_reason() is set. Drained by
        # _drain_funds_blocked_queue() the moment _check_funds_exhaustion_
        # status notices the reason clear (sw_api.py's own probe loop is
        # what makes that happen at all while everything else is paused).
        self.funds_blocked_queued_turns: list[dict[str, Any]] = []

        # activity / hang tracking
        self.last_activity = time.monotonic()
        self.last_user_activity = time.monotonic()
        self.has_seen_init = False
        # Bug fix (2026-10-16), confirmed live, a real incident (tab 1, stuck past 30
        # minutes, hang_count never left 0): HANG_ABSOLUTE_CEILING_MS's own backstop
        # (see that constant's comment -- a process can show just enough CPU/IO
        # activity, likely its own internal retry loop, to never trip the activity
        # signal) is checked ONLY against turn_pending_since -- a fresh session still
        # trying to reach has_seen_init, with no turn pending yet, has no absolute
        # ceiling at all today. Set alongside has_seen_init=False at the top of every
        # _run_loop iteration (same "a fresh attempt deserves its own fresh clock"
        # reasoning as last_activity's own reset there), cleared once has_seen_init
        # actually becomes True.
        self.connecting_since: float | None = None
        self.hang_interrupted_at: float | None = None
        self.hang_count = 0
        # Bug fix (2026-09-10): confirmed live -- interrupt() (called by
        # _check_hang on a genuine hang) is a soft ask to the CLI, not a
        # hard kill -- the CLI still sends a final ResultMessage for the
        # turn it just aborted, structurally identical to one for a turn
        # that finished normally. Treating it as real completion wiped
        # turn_pending/pending_user_text AND reset silent_turn=True before
        # _handle_failure's own replay (which fires right after, once the
        # stream then actually ends) ever got a chance to run -- confirmed
        # live: the watchdog correctly caught a genuinely slow tool call,
        # interrupted it, and the replay then genuinely succeeded (a real
        # GitLab repo really did get created a few tool calls later), but
        # the success reply never reached the user at all, silently
        # swallowed by silent_turn still being True from the misclassified
        # ResultMessage. Set right before calling interrupt(); the very
        # next ResultMessage this flag is armed for skips the normal
        # turn-pending-clear/silent_turn-reset entirely (same shape as
        # ignore_next_result_recovery, a different trigger).
        self.hang_interrupt_result_pending = False
        # What tool call (if any) was actually in flight when a hang got
        # detected -- captured so _handle_failure's replay nudge can tell
        # the model specifically what got force-interrupted (per explicit
        # instruction, 2026-09-10) instead of a generic "something failed"
        # note, so it can try a different approach instead of blindly
        # repeating the same slow/stuck call.
        self.last_tool_use_name: str | None = None
        self.last_tool_use_started_at: float | None = None
        self.hang_interrupted_tool_name: str | None = None
        self.hang_interrupted_tool_elapsed_s: float | None = None

        # Bug fix (2026-09-15, "Стоп должен срабатывать ВСЕГДА"): the real
        # OS pid of the currently-live CLI subprocess, captured directly at
        # spawn time via session_context's cli-pid-sink (set right before
        # each self.client.connect() below) -- see
        # session_context.get_cli_pid_sink's own docstring for why this
        # replaced introspecting client._transport._process.pid at
        # stop-time, which was confirmed live to sometimes come back None
        # exactly when it was needed most (force_kill_cli_process_no_pid,
        # 2026-09-15 incident). Reset to None whenever the client that owns
        # it is torn down so a stale pid from an already-dead process is
        # never targeted.
        self._cli_process_pid: int | None = None
        # Bug fix (2026-09-15), per explicit instruction (see _check_hang's
        # own comment for the full reasoning): the real hang-detection
        # liveness signal, built on the same pid capture above. Recreated
        # whenever _cli_process_pid changes (a fresh connection); None
        # until the first pid is captured.
        self._process_activity_monitor: ProcessActivityMonitor | None = None

        # silent user-wait nudge (see SILENT_USER_WAIT_NUDGE_MS) -- tracks
        # only REAL user-typed messages (submit()'s is_real_user=True),
        # not proactive/reminder/retry turns
        self.last_real_user_turn_at: float | None = None
        # Bug fix (2026-09-16): wall-clock (epoch-ms) twin of the monotonic
        # field above -- see its own comment at the real submit() call
        # site for why a separate one is needed (transcript timestamps on
        # disk are wall-clock, not process-uptime-relative).
        self.last_real_user_turn_started_at_ms: float | None = None
        self.real_user_turn_answered = False
        self.silence_nudge_sent_for_turn = False

        # Ground-truth tool-outcome tracking for the self-check/push-through
        # mechanism (2026-09-22, per explicit instruction: "Кэролайн должна
        # не просто формально 'завершать ход', она должна решать задачу до
        # конца"). Scoped to the whole real-user EPISODE (reset only by a
        # fresh real submit(), same lifetime as real_user_turn_answered right
        # above), not per-turn like turn_saw_any_visible_text -- an internal
        # follow-up check is itself a new "turn" from submit()'s point of
        # view, and the whole point is remembering a tool failure ACROSS
        # those follow-ups until it's genuinely resolved (a later call to the
        # SAME tool succeeding overwrites/clears the earlier failure -- see
        # turn_tool_outcomes' own key). _pending_tool_calls is the id->(name,
        # arguments_preview) scratch space bridging a ToolUseBlock to its eventual
        # ToolResultBlock; population happens once, at the top of _run_loop's
        # per-message handling, off the raw SDK objects both engines produce
        # identically -- see ToolOutcome's own docstring. arguments_preview added
        # 2026-10-17 so the progress narrator can see what a running call is
        # actually doing (see _narration_action_facts), not just its name.
        self._pending_tool_calls: dict[str, tuple[str, str]] = {}
        self.turn_tool_outcomes: dict[str, ToolOutcome] = {}
        # Every narration remark actually SENT this real-user episode (2026-10-11,
        # ported from a teammate's branch) -- lets the drafting prompt see its own
        # earlier remarks so it doesn't restate the same status, and lets the very
        # first remark of an episode say plainly that work has started instead of
        # finding "nothing concrete" to say. Reset alongside _pending_tool_calls/
        # turn_tool_outcomes at the start of a new real-user episode (same scope).
        self.turn_narration_remarks: list[str] = []

        # periodic mid-turn progress narration (see PROGRESS_NARRATION_INTERVAL_MS)
        # -- when a real user's own question was last actually shown something
        # (a real reply OR a generated stand-in progress comment), and what
        # that question was, so a comment (if generated) can tie back to it.
        self.last_visible_output_at: float | None = None
        # Bug fix (2026-09-22): see NARRATION_FAILURE_RETRY_S's own comment
        # -- a fully-failed generation attempt (all retries exhausted)
        # gets this short cooldown instead of either hammering the SW API
        # on the very next 5s watchdog tick or silently eating a full
        # extra 60s on top of the one that already produced nothing.
        self.last_narration_failed_at: float | None = None
        self.last_real_user_question: str | None = None
        # Set by main.py when it resumes a turn a restart interrupted: the user's ORIGINAL words. While set,
        # a resumed turn that ends without any visible text (a silent [[NO_UPDATE]]) is re-asked --
        # see the result handler and RESUMED_ANSWER_MAX_NUDGES.
        self.resumed_unanswered_question: str | None = None
        self.resumed_answer_nudges: int = 0
        # Per explicit instruction (2026-10-11): set by _check_permission_guard() whenever
        # turn.permission.guard (reforce-microagents) finds an unanswered yes/no permission
        # question, or a communication/financial action lacking a specific prior approval,
        # outstanding in the recent dialogue. None means nothing is currently blocking.
        # Consumed by _on_permission_block_checked (registered with session_context's
        # register_permission_blocker), which stops the whole turn the next time ANY tool
        # call is attempted while this is set -- see app/operations.py's dispatch().
        self.pending_unapproved_action: str | None = None
        # Per explicit instruction (2026-10-13): counts consecutive permission-guard
        # blocks on the SAME pending action (see permission_guard_last_reason below --
        # 2026-10-15 bug fix: this used to reset on every genuine real-user message,
        # see submit()'s own prior comment, which meant a user's own CLARIFICATION of
        # the still-same pending action earned the model a fresh grace cycle it kept
        # failing anyway). The first block just asks the MODEL to ask the
        # user and wait (PERMISSION_NOTE_TEXT_TEMPLATE) -- confirmed live the model can
        # ignore that and retry the same blocked action anyway (a real incident: 8
        # straight blocked attempts to email a third party, ~3 minutes, zero actual
        # questions reaching the user). _on_permission_block_checked uses this counter
        # to escalate: a SECOND consecutive block means the model didn't listen, so the
        # guard asks the user directly itself instead, and the turn stays fully stopped.
        self.permission_guard_consecutive_blocks: int = 0
        # Per explicit instruction (2026-10-15): the reason string of this session's
        # own last block, if any -- passed to turn.permission.guard as
        # previous_block_reason so IT (never a hardcoded string/slug comparison here)
        # judges whether a newly-flagged action is the same one still pending or a
        # genuinely different one; see _check_permission_guard's own update.
        self.permission_guard_last_reason: str | None = None
        # Per explicit instruction (2026-10-18), the action-boundary tracking
        # system: the open-operations registry _check_action_before_send
        # resolves/matches against (point 1), flags approved/revoked after
        # every real user message (point 3), and PermissionGuard is skipped
        # entirely for (point 4). Persisted to disk per tab (the user's
        # explicit choice) -- loaded once here, written on every mutation.
        self.action_registry: ActionRegistry = ActionRegistry.from_wire(load_action_state(workspace_dir, tab_id))
        # The action id _check_action_before_send resolved for the send-type
        # call currently in flight, if any -- read (and cleared) by
        # _on_action_send_completed once that call's own handler succeeds.
        # None whenever no send-type call is in flight, or none had an
        # action tied to it (point 6: an action needing no separate
        # sanction never sets this, same as today).
        self._action_id_for_pending_send: str | None = None
        # Bug fix (2026-09-14), per explicit instruction, root-caused via a
        # real live incident ("Кэролайн ведет беседу сама с собой"):
        # last_visible_output_at gets bumped by narration's OWN firing too
        # (it has to, to throttle to one comment per PROGRESS_NARRATION_
        # INTERVAL_MS) -- so it can't tell "a real reply just happened" from
        # "narration itself just fired" apart. Confirmed live: when a real
        # turn stays stuck for many minutes with no genuine progress,
        # _check_progress_narration kept firing every minute regardless,
        # each call re-narrating the same stale, unchanging dialogue window
        # -- individually novel enough to dodge the echo/garbage filters,
        # but collectively a nonsense stream of paraphrases that read as
        # Caroline talking to herself. This counter is separate and STRICT:
        # incremented only by narration actually firing, reset to 0 only by
        # genuine progress (a real user submit() or the real model's own
        # visible output, see MAX_CONSECUTIVE_NARRATION_COMMENTS's call
        # sites) -- never by narration's own output, which is the one
        # thing it exists to cap.
        self.consecutive_narration_count = 0

        # Voice narration (see VOICE_NARRATION_TICK_S). turn_seq ties a drafting task to
        # the turn it was started for, so a draft finishing after its turn ended is dropped.
        self._voice_narration_task: asyncio.Task[None] | None = None
        self._voice_narration_wake = asyncio.Event()
        # Bumped by stop() (user Stop and permission-guard stop both go through
        # it -- see its own comment) so _check_progress_narration's staleness
        # check can drop a comment that was already mid-generation when the
        # stop landed, even if pending_user_text/turn_pending end up looking
        # unchanged by the time it would otherwise send (2026-10-16 incident:
        # a narration generated for a turn killed by the permission guard was
        # sent a second AFTER the guard's own direct question, flatly
        # contradicting it).
        self._narration_stop_epoch = 0
        self._narration_turn_seq = 0
        self._narration_turn_started_at: float | None = None
        self._narration_draft_task: asyncio.Task[None] | None = None
        self._prepared_narration: tuple[str, float, str | None] | None = None
        # Seconds a voice remark has been taking from drafting to ready, averaged across
        # turns (see _stale_lead); None until one has been measured.
        self._narration_prep_s: float | None = None
        # The remark the fast lane's decline came with (see _say_first_voice_remark). Set
        # as soon as it is known, before it is synthesized, so that the loop doesn't
        # draft a "work has started" of its own beside it.
        self._first_voice_remark: str | None = None
        self._first_voice_remark_covers_start = False
        self._first_voice_remark_task: asyncio.Task[None] | None = None
        self._prepared_narration_is_first = False
        # Whether the prepared remark was drafted with nothing new to tell, and how many such
        # remarks have gone out in a row (see VOICE_NARRATION_STALE_STEP_S).
        self._prepared_narration_stale = False
        self._stale_remarks_in_row = 0
        # Fillers (app/filler_phrases.py) said since the last real remark.
        self._fillers_in_row = 0
        # Lines of activity since a tier's answer went to the verifier; None when no answer
        # is being checked (see _answer_being_checked).
        self._lines_since_answer_candidate: int | None = None
        self._held_for_answer: str | None = None
        self._narration_facts_at_last_draft: str | None = None
        self._narration_last_draft_at: float | None = None
        self._voice_narration_failed_at: float | None = None
        # chat.js's own speech queue (speech_state control op). None until it first reports.
        self._client_speaking: bool | None = None
        self._client_quiet_since: float | None = None
        self._client_speech_reported_at: float | None = None
        # True while _client_speaking is only our own assumption after a send, not chat.js's report.
        self._speaking_assumed = False
        self._last_narration_est_speech_s = 0.0

        # restart budget
        self.restart_timestamps: list[float] = []

        # session-id tracking. Context ageing is Claude's own native
        # auto-compaction now (see _run_loop's options: autoCompactEnabled)
        # -- no per-turn transcript rewrite, no per-turn CLI restart.
        self.last_saved_session_id: str | None = None

        self.current_chat_source: str | None = None
        # Per explicit instruction (2026-09-14): path to this tab's rolling
        # 24h-dialogue file -- see recent_dialogue_history_instruction's
        # own docstring (policies.py) and _write_recent_24h_dialogue_file
        # (above) for the full feature. The PATH is stable for this tab's
        # whole lifetime (unlike _system_prompt_language below, its own
        # system-prompt mention never goes stale/needs a restart to pick
        # up a change) -- only the file's CONTENT changes, rewritten fresh
        # before every real user submit(), so the model sees current data
        # any time it actually reads the file, regardless of how long this
        # particular query() client has been alive. None until the first
        # real user turn ever runs (see recent_dialogue_history_
        # instruction's own None-safe handling).
        self._recent_24h_dialogue_file_path: str | None = None
        # Single-flight state for _schedule_recent_24h_dialogue_refresh (see
        # its own docstring) -- at most one worker-thread refresh in flight
        # per tab; a request that arrives meanwhile just sets _dirty so one
        # more pass runs afterward, instead of piling up N parallel readers.
        self._recent_24h_refresh_task: "asyncio.Task[None] | None" = None
        self._recent_24h_refresh_dirty = False
        # The language hint baked into the CURRENT query()'s system prompt.
        # A long-lived client doesn't re-read it every turn anymore, so a
        # real user turn that finds the persisted language has changed
        # sets restart_pending to pick the new one up (see submit()).
        self._system_prompt_language: str | None = None

        # Consecutive "authentication_failed" api_retry messages on the
        # CURRENT connection -- see AUTH_RETRY_ESCALATION_THRESHOLD below.
        self.consecutive_auth_retry_failures = 0

        # deliberate-restart flags. restart_pending is the generic "tear the
        # query down and rebuild it cleanly next _run_loop iteration" signal
        # (auth-failure escalation, a settings/language change).
        self.restart_pending = False
        # Bug fix (2026-09-18), confirmed live: clear_tab()'s own kill
        # sometimes produces a normal, non-exceptional trailing
        # ResultMessage (the CLI winds down gracefully instead of the
        # connection just dying) -- without this, that ResultMessage was
        # treated as a genuine turn completion, which fired _fire_post_
        # turn_completion_check()'s "did you actually finish?" nudge
        # against the now-EMPTY freshly-cleared session. The model had
        # nothing to reference, produced SOME visible reply to it anyway,
        # and that reply became the first thing shown in the "cleared"
        # tab -- looking exactly like the clear had silently failed. Same
        # result_is_fake shape as hang_interrupt_result_pending/
        # forced_compaction_result_pending, one more reason a trailing
        # ResultMessage must never be treated as real.
        self.clear_tab_result_pending = False
        self.restart_for_unrecoverable_session = False
        self.unrecoverable_session_replay_text: str | None = None
        self.unrecoverable_session_replay_attachments: list[Any] = []
        self.skip_migration_fallback_once = False

        # Cross-engine handoff framing (2026-09-22), per explicit instruction
        # after a real incident: switch_engine_if_needed()'s force_restart()
        # ends the stream exactly like any other unexpected disconnect, so it
        # landed in _handle_failure's generic "internal failure, infrastructure
        # self-healing" framing -- which is simply WRONG for a deliberate,
        # user-requested engine switch, and (confirmed live) gives the newly
        # active engine no hint that it just inherited a conversation from a
        # DIFFERENT engine with none of its own memory of it. Set right before
        # force_restart() in switch_engine_if_needed(), consumed once (and
        # reset to None) at the top of _handle_failure, which swaps in a
        # dedicated note instead of the generic one when this is populated --
        # see _handle_failure's own comment for the incident this fixes.
        self.restart_engine_switch: tuple[str, str] | None = None

        # conn state / api retry / rate limit memory
        self.conn_state: dict[str, Any] = {"kind": "connected"}
        self.ignore_next_result_recovery = False
        # Bug fix (2026-09-22), confirmed live: the cc_cli_limit_message branch
        # below deliberately does NOT set ignore_next_result_recovery (per the
        # 2026-09-11 fix's own reasoning -- a real AssistantMessage came
        # through, so turn_pending must clear normally, not pretend the turn
        # never happened). But that flag ALSO gates the generic "reset
        # conn_state to connected" a little further down -- so conn_state,
        # set to "limited" moments earlier for a real, still-active usage
        # limit, was being silently overwritten back to "connected" by that
        # SAME turn's own trailing ResultMessage well under a second later.
        # Confirmed live: the status lamp/text never had a real chance to
        # show it. A narrower, single-purpose flag: protects ONLY the
        # conn_state reset for the one ResultMessage immediately following a
        # real limit hit, leaving turn_pending/the retry timer completely
        # unaffected (a genuine fix, not a revert of the 2026-09-11 one).
        self.suppress_next_conn_state_reset = False
        self.api_retry_timer: asyncio.TimerHandle | None = None
        # See _schedule_one_shot_followup_check's own doc comment -- a
        # separate timer from api_retry_timer above, deliberately never
        # re-armed by its own firing (unlike api_retry_timer, which keeps
        # rescheduling itself).
        self.one_shot_followup_timer: asyncio.TimerHandle | None = None
        self.last_rate_limit_info: dict[str, Any] | None = None
        self.last_api_retry_error: str | None = None
        self.mcp_reconnect_timers: dict[str, asyncio.TimerHandle] = {}

        self._run_loop_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None

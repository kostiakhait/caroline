"""Run with any Python 3.12+, no network and no account: python narrator_scheduler_sim.py

Exercises the voice-narration scheduler extracted verbatim from chat_session.py,
with the model, the client's speech queue and the turn simulated. Time constants are
scaled down (SCALE) so a multi-minute turn runs in seconds; all checks are in the
scaled units converted back to real seconds."""
import ast, asyncio, os, sys, time, types

# Split 2026-10-17: the voice-narration methods/constants this sim extracts
# verbatim used to all live in chat_session.py itself. They now live in
# chat_session_shared.py (module-level constants/helpers, imported by every
# mixin) and app/chat_session_mixins/{narration_text,narration_voice}.py
# (the methods, split by subsystem -- see those mixins' own ChatSession.__mro__
# for how they recombine at runtime). Read from all three.
APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
SHARED_SRC = os.path.join(APP_DIR, "chat_session_shared.py")
MIXIN_SRCS = [
    os.path.join(APP_DIR, "chat_session_mixins", "narration_text.py"),
    os.path.join(APP_DIR, "chat_session_mixins", "narration_voice.py"),
]
SRC = SHARED_SRC  # used below only as the `filename` compile() reports on error
SCALE = 0.05  # 1 real second -> 0.05 s of simulation

shared_tree = ast.parse(open(SHARED_SRC, encoding="utf-8").read())
consts = {}
for node in shared_tree.body:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        name = node.targets[0].id
        if name.startswith(("VOICE_NARRATION_", "NARRATION_")) or name == "_INTERIM_NOTES_HEADER":
            consts[name] = ast.literal_eval(node.value)

WANTED = {"_voice_narration_applies", "_reset_voice_narration", "on_client_speech_state", "_client_quiet_for",
          "_voice_narration_loop", "_step_voice_narration", "_draft_voice_narration", "_send_narration_remark",
          "_narration_task", "_narration_action_facts", "_synthesize_narration_audio", "_draft_voice_narration_body",
          "_say_first_voice_remark", "_review_and_synthesize", "_stale_interval"}
# Module-level helpers (now in chat_session_shared.py) the copied methods call.
HELPERS = {"_spoken_duration"}
helpers = [n for n in shared_tree.body if isinstance(n, ast.FunctionDef) and n.name in HELPERS]
assert {h.name for h in helpers} == HELPERS, HELPERS
funcs = []
for mixin_src in MIXIN_SRCS:
    mixin_tree = ast.parse(open(mixin_src, encoding="utf-8").read())
    cls = next(n for n in mixin_tree.body if isinstance(n, ast.ClassDef) and n.name.endswith("Mixin"))
    funcs.extend(n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in WANTED)
missing = WANTED - {f.name for f in funcs}
assert not missing, missing

# ---- stubs the extracted methods reference -------------------------------------
events = []          # (sim_time, kind, detail)
T0 = time.monotonic()
def now_s(): return (time.monotonic() - T0) / SCALE
def log_event(cat, name, **kw):
    if name.startswith("voice_narration"):
        events.append((now_s(), name, kw))

state = {"model_delay": 3.0, "empty": False, "calls": 0}
async def generate_progress_comment(dialogue, language, **kw):
    state["calls"] += 1
    state["live"] = state.get("live", 0) + 1; state["max_live"] = max(state.get("max_live", 0), state["live"])
    await asyncio.sleep(state["model_delay"] * SCALE)
    state["live"] -= 1
    if state["empty"]:
        return ""  # the prompt's own "nothing new" reply
    return f"remark#{state['calls']} facts={kw['activity'].splitlines()[-1]} said={len(kw['already_said'])}"

voice_api = types.ModuleType("app.plugins.voice_api"); voice_api.generate_progress_comment = generate_progress_comment
async def clean_text_for_speech(t): return t
async def synthesize_speech(t, voice, language=None):
    state["live"] = state.get("live", 0)
    state["tts"] = state.get("tts", 0) + 1
    state["live"] += 1; state["max_live"] = max(state.get("max_live", 0), state["live"])
    await asyncio.sleep(1.5 * SCALE); state["live"] -= 1; return "AUDIO"
voice_api.clean_text_for_speech = clean_text_for_speech; voice_api.synthesize_speech = synthesize_speech
voice_api.voice_for_gender = lambda g: "Nova"
# reply.review for a voice remark (VOICE_NARRATION_REVIEW). REVIEW_S=<seconds> gives it the
# server's real latency (2.2-3.8s measured) to see what that does to the schedule; the
# default 0 keeps the checks below about the scheduler alone.
REVIEW_S = float(os.environ.get("REVIEW_S", "0"))
async def review_reply(text, task, **kw):
    await asyncio.sleep(REVIEW_S * SCALE)
    return text
voice_api.review_reply = review_reply
async def generate_progress_comment_direct(task, activity, already_said, language, resolve, **kw):
    return await generate_progress_comment('', language, activity=activity, task=task, already_said=already_said)
voice_api.generate_progress_comment_direct = generate_progress_comment_direct
async def draft_narration_remark(task, facts, already_said, language, **kw):
    return await generate_progress_comment('', language, activity=facts, task=task, already_said=already_said)
sys.modules["app"] = types.ModuleType("app"); sys.modules["app.plugins"] = types.ModuleType("app.plugins")
sys.modules["app.plugins.voice_api"] = voice_api

ns = {"asyncio": asyncio, "time": time, "log_event": log_event,
      "current_language_name": lambda tab: "Russian", "get_persona_gender": lambda d: "female",
      "get_owner_profile": lambda d: types.SimpleNamespace(gender=None), "Any": object, "draft_narration_remark": draft_narration_remark, **consts}
for k, v in list(consts.items()):  # scale every time constant
    if k == "VOICE_NARRATION_EST_CHARS_PER_S":
        ns[k] = v / SCALE  # a rate, not a duration
    elif k.endswith("_S"):
        ns[k] = v * SCALE
mod = ast.Module(body=funcs, type_ignores=[])
code = compile(ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name="S", bases=[], keywords=[], body=funcs, decorator_list=[], type_params=[])], type_ignores=[])), SRC, "exec")
exec(code, ns)
exec(compile(ast.fix_missing_locations(ast.Module(body=helpers, type_ignores=[])), SRC, "exec"), ns)
S = ns["S"]

class Client:
    """chat.js's queue: plays remarks one at a time, reporting speech_state."""
    def __init__(self, sess, reports=True, speak_s_per_char=1 / 14):
        self.sess, self.reports, self.rate = sess, reports, speak_s_per_char
        self.queue, self.running, self.spoken = [], False, []
    async def receive(self, text, narration):
        if not narration:
            self.queue = [q for q in self.queue if not q[1]]
        self.queue.append((text, narration))
        if not self.running:
            asyncio.create_task(self.run())
    async def run(self):
        self.running = True
        if self.reports: self.sess.on_client_speech_state(True)
        while self.queue:
            text, narration = self.queue.pop(0)
            start = now_s()
            await asyncio.sleep((0.8 + len(text) * self.rate) * SCALE)  # 0.8s synthesis + speaking
            self.spoken.append((start, now_s(), text, narration))
        self.running = False
        if self.reports: self.sess.on_client_speech_state(False)

def make_session(voice=True, reports=True):
    s = S()
    s.ended = False; s.tab_id = "t"; s.workspace_dir = "."
    s.small_model_active = True; s.turn_is_voice = voice; s.turn_pending = True
    s.conn_state = {"kind": "connected"}; s.current_turn_suppress_narration = False
    s.small_model_activity = []; s._pending_tool_calls = {}; s.turn_tool_outcomes = {}
    s.last_real_user_question = "найди письмо от Анны про счёт"
    s.last_visible_output_at = None; s.consecutive_narration_count = 0; s.pending_interim_notes = []
    s._voice_narration_wake = asyncio.Event(); s._narration_turn_seq = 0
    s._narration_draft_task = None; s.turn_narration_remarks = []
    s._prepared_narration_stale = False; s._stale_remarks_in_row = 0
    s._client_speaking = None; s._client_quiet_since = None; s._client_speech_reported_at = None
    s._last_narration_est_speech_s = 0.0
    s._first_voice_remark_task = None
    s._reset_voice_narration()
    s._narration_turn_started_at = time.monotonic()
    s.client = Client(s, reports)
    async def send(env):
        text = env["message"]["message"]["content"][0]["text"]
        if env.get("isNarration"): state.setdefault("with_audio", []).append(bool(env.get("audioBase64")))
        await s.client.receive(text, env.get("isNarration", False))
    s.send = send
    return s

async def scenario(name, *, turn_s, fact_every=None, voice=True, reports=True, model_delay=3.0, empty=False, first_remark_at=None):
    events.clear(); state.update(model_delay=model_delay, empty=empty, calls=0, with_audio=[], live=0, max_live=0)
    global T0; T0 = time.monotonic()
    s = make_session(voice, reports)
    loop = asyncio.create_task(s._voice_narration_loop())
    async def facts():
        i = 0
        while True:
            i += 1
            s.small_model_activity.append(f"fact {i}")
            if not fact_every: return
            await asyncio.sleep(fact_every * SCALE)
    ft = asyncio.create_task(facts())
    if first_remark_at is not None:
        async def first():
            await asyncio.sleep(first_remark_at * SCALE)
            s._first_voice_remark = 'FIRST REMARK from the decline'
            s._first_voice_remark_covers_start = True
            s._first_voice_remark_task = asyncio.create_task(s._say_first_voice_remark(s._narration_turn_seq, s._first_voice_remark, None))
        asyncio.create_task(first())
    await asyncio.sleep(turn_s * SCALE)
    # final answer: same order as _finish_small_model_turn_answered
    s._reset_voice_narration(); answer_at = now_s()
    await s.send({"message": {"message": {"content": [{"text": "FINAL ANSWER"}]}}})
    s.small_model_active = False; s.turn_pending = False
    ft.cancel()
    await asyncio.sleep(12 * SCALE)
    s.ended = True; loop.cancel()
    return s, answer_at

def report(name, s, answer_at, checks):
    spoken = s.client.spoken
    print(f"\n=== {name} ===")
    for a, b, text, narr in spoken:
        print(f"  {a:6.1f}-{b:6.1f}s  {'NARR ' if narr else 'REAL '} {text}")
    sent = [e for e in events if e[1] == "voice_narration_sent"]
    print(f"  remarks sent: {len(sent)}, model calls: {state['calls']}, final answer at {answer_at:.1f}s")
    ok = True
    for desc, cond in checks(spoken, sent, answer_at):
        print(f"  [{'OK' if cond else 'FAIL'}] {desc}")
        ok &= cond
    return ok

def no_overlap(spoken):
    return all(spoken[i][1] <= spoken[i + 1][0] + 1e-6 for i in range(len(spoken) - 1))

def gaps(spoken):
    return [spoken[i + 1][0] - spoken[i][1] for i in range(len(spoken) - 1)]

async def main():
    all_ok = True

    s, ans = await scenario("A: 60s voice turn, a new fact every 4s", turn_s=60, fact_every=4)
    def ca(sp, sent, ans):
        narr = [x for x in sp if x[3]]
        g = gaps(narr)
        return [
            ("no two lines ever overlap", no_overlap(sp)),
            ("first remark not before 5s", bool(narr) and narr[0][0] >= 5 - 0.5),
            ("first remark within 8s", bool(narr) and narr[0][0] <= 8),
            (f"gaps between remarks all <= 4s (max {max(g, default=0):.1f}s)", all(x <= 4 for x in g)),
            ("final answer is spoken", any(not x[3] for x in sp)),
            ("every remark arrives with ready audio", all(state.get("with_audio", [False]))),
            (f"never two drafts at once (max {state['max_live']})", state["max_live"] == 1),
            ("nothing narrated after the final answer started", all(x[0] <= ans + 0.5 for x in narr)),
        ]
    all_ok &= report("A", s, ans, ca)

    s, ans = await scenario("B: answer lands while a draft is in flight", turn_s=7, fact_every=1, model_delay=6)
    def cb(sp, sent, ans):
        return [
            ("the in-flight draft is never spoken (cancelled with the turn)", state["calls"] == 1 and not any(x[3] for x in sp)),
            ("final answer is spoken", any(not x[3] for x in sp)),
        ]
    all_ok &= report("B", s, ans, cb)

    s, ans = await scenario("C: facts stop changing", turn_s=60, fact_every=None)
    def cc(sp, sent, ans):
        narr = [x for x in sp if x[3]]
        starts = [x[0] for x in narr]
        sp_gaps = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
        return [
            (f"stale remarks spaced >= 10s (starts {[round(x) for x in starts]})", all(x >= 10 - 0.5 for x in sp_gaps)),
            ("still some narration (not silent for a minute)", len(narr) >= 2),
            ("each later draft saw the earlier remarks", all(f"said={i}" in narr[i][2] for i in range(len(narr)))),
        ]
    all_ok &= report("C", s, ans, cc)

    s, ans = await scenario("D: old client, no speech_state reports", turn_s=40, fact_every=4, reports=False)
    def cd(sp, sent, ans):
        return [("no two lines ever overlap (estimate-paced)", no_overlap(sp)), ("narrated at all", any(x[3] for x in sp))]
    all_ok &= report("D", s, ans, cd)

    s, ans = await scenario("E: text-only turn", turn_s=30, fact_every=4, voice=False)
    def ce(sp, sent, ans):
        return [("voice loop stays out of text turns", not any(x[3] for x in sp) and state["calls"] == 0)]
    all_ok &= report("E", s, ans, ce)

    s, ans = await scenario("F: model keeps returning nothing", turn_s=30, fact_every=4, empty=True)
    def cf(sp, sent, ans):
        return [(f"'nothing new' is not retried: {state['calls']} model calls in 30s (<= 8)", state["calls"] <= 8)]
    all_ok &= report("F", s, ans, cf)

    s, ans = await scenario("G: the decline came with a first remark, then no new facts", turn_s=40, fact_every=None, first_remark_at=0.2)
    def cg(sp, sent, ans):
        narr = [x for x in sp if x[3]]
        starts = [x[0] for x in narr]
        return [
            ("the first thing spoken is the decline's remark", bool(narr) and narr[0][2].startswith("FIRST")),
            (f"and it starts within 3.5s, not after 5s (at {starts[0] if starts else -1:.1f}s)", bool(narr) and starts[0] <= 3.5),
            (f"no second 'work has started': next remark only by the stale rule, >= 10s later (starts {[round(x) for x in starts]})",
             len(starts) < 2 or starts[1] - narr[0][1] >= 10 - 0.5),
            ("later drafts know the first remark was said", all("said=0" not in x[2] for x in narr[1:])),
            ("no two lines ever overlap", no_overlap(sp)),
        ]
    all_ok &= report("G", s, ans, cg)

    s, ans = await scenario("H: first remark, then a new fact every 4s", turn_s=40, fact_every=4, first_remark_at=0.2)
    def ch(sp, sent, ans):
        narr = [x for x in sp if x[3]]
        g = gaps(narr)
        return [
            ("the first thing spoken is the decline's remark", bool(narr) and narr[0][2].startswith("FIRST")),
            ("the narrator carries on from real facts", len(narr) >= 4),
            (f"gaps between remarks all <= 4.5s (max {max(g, default=0):.1f}s)", all(x <= 4.5 for x in g)),
            ("later drafts know the first remark was said", all("said=0" not in x[2] for x in narr[1:])),
            ("no two lines ever overlap", no_overlap(sp)),
        ]
    all_ok &= report("H", s, ans, ch)

    s, ans = await scenario("I: the answer lands before the first remark's audio is ready", turn_s=1.0, fact_every=None, first_remark_at=0.2)
    def ci(sp, sent, ans):
        return [("the remark is never spoken", not any(x[3] for x in sp)), ("final answer is spoken", any(not x[3] for x in sp))]
    all_ok &= report("I", s, ans, ci)

    print("\nALL OK" if all_ok else "\nSOME CHECKS FAILED")

asyncio.run(main())

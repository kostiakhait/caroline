# Continuous narration: check, fix, measure the cost

Author: Claude (with Konstantin Khait) / Version: 1 / Date: 2026-10-23 / Company: Componentality / Project: Caroline

## 1. Requirement (as stated, 2026-10-23)

For ANY request of the user, Caroline reacts with a continuous stream of spoken remarks. The user must not
wait more than 3 s between two remarks (never more than 5 s with nothing heard). The same in audio-only mode
and in the animated mode (Visual Mode, the talking head). Original wording of the idea (2026-10-09):
"непрерывный диалог, чтобы реплики не накладывались одна на другую, но при этом между ними не было
значимых пауз. И чтобы они были осмысленными".

## 2. What the code does today (read, nothing run)

- One loop for both modes: `_voice_narration_loop` (narration_voice.py). Visual Mode has no loop of its own: the
  backend sends the remark (text + synthesized audio); chat.js plays it, in Visual Mode by handing the audio to
  the native talking-head renderer (a clip renders in 0.5-0.8 s).
- The loop runs only for an **sw-mode voice turn** (`_voice_narration_applies`). A claude-mode turn and a text turn
  get the old once-a-minute remark (`PROGRESS_NARRATION_INTERVAL_MS = 60 s`, narration_text.py).
- A remark = narrator call (Haiku 4.5, ~1.8k-token prompt) -> `reply.review` (microagent, up to 6 s) -> local
  edge-tts (free). It is prepared while the previous remark is still being spoken.
- The schedule: no remark before 5 s of the turn (`VOICE_NARRATION_FIRST_S`); breath 1.2 s after a clip; with
  no new fact the next remark waits **10 s, growing by 2 s per remark up to 14 s** (`VOICE_NARRATION_STALE_*`),
  and then draft + review + speech add 4-8 s. Designed silence is therefore 10-20 s, the requirement is 3 s.
- The narrator is allowed to speak only about real facts (`small_model_activity`, last 8 lines) and answers
  `<narration></narration>` (nothing) when it has none. A repeat of a recent remark is rejected.
- The offline simulator `evals/narrator_scheduler_sim.py` checks the schedule against a 4.5 s limit with a
  model delay of 3 s; its limit does not match the current constants.
- The backend learns about playback only from `speech_state` (speaking true/false). That flag stays true while the
  client queue is non-empty, even if the next item is still being synthesized or rendered, so the backend
  cannot see real silence between two queued items.
- No voice-narration events at all in the local log for 21-23 Oct: no data on real gaps exists.

## 3. Gaps against the requirement

1. Silence is designed to be 10-20 s (stale rule), not <= 3 s.
2. Only sw-mode voice turns. Claude-mode (SDK) turns: one remark a minute.
3. Content: with no new fact the narrator has nothing true to say, so the schedule falls silent by design.
4. Cost: the narrator prompt is ~1.8k tokens per remark. A remark every ~4 s means ~15 calls a minute: probably more
   than the cost of the work itself (the cheapest tasks in the sw battery cost $0.003-0.04 in total).
5. No measurement of what the user really hears (silence between clips, time to the first word, the join between
   the last remark and the answer).

## 4. Measurement (before any change)

Metrics per turn, in both modes: time to the first word after the user's message; every silence between the end of
a clip and the start of the next (max, p95, count over 3 s and over 5 s); silence between the last remark and
the answer; number of remarks; narration cost (Haiku calls + `reply.review` calls) per minute and as a share of
the turn's total cost.

How:
- Client instrumentation (small, chat.js): log the real start and end of every clip (`playOneSpeech`) and send it to
  the backend as an event, so gaps are measured where the user hears them, including synthesis and render waits.
- Cost: sum from the Camerlengo OpenRouter log over the turn window (same method as the sw battery), narrator
  and review calls separately.

## 5. Test bench (disposable DigitalOcean droplet only)

- A fresh droplet by the same recipe as the sw battery; the harness sends **voice** turns (`turn_is_voice`).
- A client emulator in the harness: receives the narration envelopes, takes the clip length from the audio,
  plays it in simulated time, reports `speech_state` and clip start/end. Animated mode: the same with a
  render delay of 0.8 s per clip plus the prefetch behaviour of chat.js. Real models, real TTS, real tools.
- Animated mode must run for real on the droplet too (decision 2026-10-23: "если нельзя - это баг и надо делать,
  чтобы было можно"). Why it does not today: it is not a platform limit. The talking head is rendered by
  `vendor/XcfaRenderer`, a C# library (SkiaSharp + OpenCvSharp, both ship Linux builds; the only Win32 code is a
  small `kernel32` call in `ProcessWatchdog.cs`). The Windows shell calls it in-process from `VisualModeManager.cs`;
  the Linux shell is Python, there is no bridge, so on Linux `visual_speech_audio` answers `played: false`
  (LINUX_PORT_PLAN.md, Phase 4 finding). Work item: a headless .NET bridge process (audio in -> video out, the same
  `visual_speech_prepare` / `visual_speech_audio` / `visual_speech_cancel` messages), run by the Linux shell and
  by the emulator. The WPF window (playback, borderless, topmost) is not needed for the measurement: the render
  time and the join between clips are.
  Until the bridge exists, the emulator uses a render delay of 0.8 s per clip (measured on Windows).
- Task set (voice): a quick answer (< 5 s, narration must stay out of the way); a 20 s lookup; a 60 s multi-tool
  task; a 3 min task; a task with a long silent model "thinking" phase; a task with one long tool call.

## 6. Likely changes (to be decided after the baseline, not now)

1. A continuous mode of the schedule: the next remark is always in preparation; the 10-14 s stale wait goes; a remark
   is never later than the end of the previous clip + 1-2 s.
2. Material for remarks when nothing new has happened: the plan (declare_plan steps), what is being waited for,
   what was found so far, the elapsed time, what was already ruled out. Different kinds of remarks in rotation, so
   it is not "Продолжаю..." every 3 s (the complaint of 2026-10-15).
3. Longer remarks (~15-25 words): speaking time covers preparation time, so fewer calls are needed for the same
   continuity. Pipeline: remark N+1 is drafted the moment N is sent.
4. Cost: a stable prompt prefix first (cache), a shorter prompt, two remarks per call, check cheaper models on the
   narrator golden set (`evals/narrator_golden.py`), `reply.review` only where it can change something.
5. Extend the loop to claude-mode turns (facts exist there too, `_narration_action_facts`).
6. Update `narrator_scheduler_sim.py` to the new limits.

## 7. Phases

0. Droplet + harness + client emulator + clip telemetry (no behaviour change).
1. Baseline: the task set above, both modes, current code. Output: gap statistics + cost per minute.
2. Decide the changes from section 6 with the numbers in hand.
3. Implement, repeat the same set, compare. Stop a run at the first wrong behaviour.
4. Report: gaps and cost before/after, per mode.
5. Separately, after the main testing is finished (decision 2026-10-23): a run and an analysis on a WEAK machine
   (for example a droplet of 1 vCPU / 2 GB). The main testing runs on an 8 vCPU / 16 GB droplet; what a slow
   machine does to render time, synthesis and the gaps is a different question and is not mixed into it.

## 7a. A fixed shift is allowed (decision 2026-10-23)

"Если у нас будет фиксированная задержка всех реплик (сдвиг) на 2-3, даже 5 секунд - это нормально, мы найдем, что с
этим делать." So the stream may be played out with a constant lag behind the work, like a jitter buffer: remarks are
drafted, reviewed, synthesized and (in animated mode) rendered ahead and played at a steady pace. What must hold is
the gap between consecutive clips (target 3 s, limit 5 s); the time to the first word may be up to ~5 s. This makes
the animated render (measured 2.8 s per clip on Windows) and the review (2-6 s) a matter of buffer depth, not of
gaps. The pass line is therefore measured from the first clip on; the time to the first word is reported separately.

## 7b. Filler remarks ("заглушки") and the continuous schedule (decisions 2026-10-24)

Measured baseline (voice turn, code task C1, no animation): gaps 2-31 s, p95 12 s, 11 of 12 over 3 s. Causes: the
schedule itself waits 10-14 s when nothing is new; the narrator often answers "nothing new" (`nothing_new`); every
remark takes 2.4-4.5 s to prepare; remarks repeat ("жду результата" three times in a row).

Decisions:
1. A pre-coded set of 60+ short filler remarks per language ("Одну секунду", "Немного задумалась", "Сейчас
   продолжу", ...), with grammatical gender of the persona (Caroline feminine, Peter masculine). A pause with nothing
   real to say is filled with a filler; never more than 4 fillers in a row, then a real remark is forced.
2. Texts: the Russian set is written by hand (gender forms marked `{ась|ся}`); English likewise. Any other language: a
   model translates the set once, at the first use, and the result is cached on disk (no fixed word lists in code).
3. Audio: ru and en are shipped pre-synthesized in the installer (`backend-py/app/assets/fillers/`); any other language
   is synthesized at first use, 8 requests in parallel, and cached on disk. Until a language's set is ready no filler
   plays in that language.
4. Animated mode: the rendered clip of a filler is cached too (per model), so a filler appears without the 2-3 s of
   rendering. Cache key: hash of the audio.
5. Schedule: a real remark is prepared while the previous one is spoken and goes out the moment it ends; a filler goes
   out when the client has been quiet for 1.5 s and no remark is ready; the narrator is asked at most once per
   `VOICE_NARRATION_STALE_S` when nothing is new (its calls cost money, a filler does not).
6. Repeats: the narrator is told each new remark must state a different fact (what is done, what is being done now,
   what is expected, how long it has taken); fillers are not shown as chat bubbles and are not part of "already said".

## 8. Decisions

Made (2026-10-23):
- Pass line: target 3 s, hard limit 5 s (p95 <= 3 s and no silence over 5 s).
- Scope: sw-mode only (claude-mode turns keep the once-a-minute remark; section 6.5 is dropped for now).
- Animated mode: run it for real on the droplet, which needs the headless renderer bridge (section 5).
- Droplet: not created yet; waiting for the go-ahead.

Still open:
- How repetitive may a stream of remarks be, if true new facts run out.
- Cost ceiling for narration (per minute or as a share of the turn).

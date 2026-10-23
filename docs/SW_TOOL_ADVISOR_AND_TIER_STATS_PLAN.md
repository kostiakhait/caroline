# sw: tool advisor and tier statistics

- **Author:** Claude (Claude Code), for Konstantin Khait
- **Version:** 0.1 (draft for approval)
- **Date:** 2026-10-22
- **Company:** Partners Solutions
- **Project:** Caroline

## Why

On the sw test box a weak tier keeps answering a mailbox task with a question ("give me the password
and servers") while the credentials are saved and one tool call would do. The verifier only decides
"done / not done"; nothing tells the retried tier *what to try*. And the cascade starts every task at
the same tier, so a tier that keeps failing a kind of task is paid for every time before escalation.

General approach (standing): many small separate microagents, each with the narrowest task, each on
the microagent ladder with a code `verify`; a failing task is split into parts, independent parts run
in parallel.

## 1. Tool advisor (the "checker that hints")

Runs when the verifier finds an attempt unfinished and the tier stays (first retry, or the progress
microagent saw progress). Its output goes into the retry nudge.

- **`sw.tools.groups`** -- input: the formalized task, the verifier's one-sentence gap, the calls
  made so far with their results, and the plugin catalog (one line per plugin + its tool count).
  Output: plugin groups ranked from most to least likely to close the gap, each with a reason.
- **`sw.tools.calls`** -- input: the task, the gap, the calls so far, and the full descriptions of
  the tools of ONE group -- the highest-ranked group not tried yet in this turn. Output: the concrete
  calls to make (tool + the arguments that matter) and which of them are independent.
- **One group per attempt.** The nudge says: "try these now: ...; the independent ones in parallel
  (declare_plan parallel stage + run_parallel_stage)". The next retry gets the next group.
- **Verifier:** keeps deciding done / not done only; its gap sentence is the advisor's input.

## 2. Tier statistics

- **`sw.task.tags`** -- free tags for a task, from the formalized request (a few short tags: what
  kind of work, what objects, which channels).
- **Recording (code):** at the end of a cascade turn, for each tag and each tier the turn ran on:
  `total += 1`; `success += 1` if the task was closed on that tier (retries included); `fail += 1`
  if it had to escalate above it (verdict or stall).
- **`sw.tier.advice`** -- input: the task's tags and the statistics table; output: the tier to start
  from and why (matching free tags by meaning, judging "fails too often" itself).
- **Start tier = the higher of** the fast lane's choice and the advice; with too little data for the
  tags, the fast lane's choice alone.

## Storage of the statistics

- **Local:** `workspace/sw-tier-stats.json` -- the totals as of the last sync plus the local delta
  (counts since then). Advice reads totals + delta.
- **Synced to Notes:** one shared table note (folder `Caroline:Stats`, note `sw-tier-stats`), on
  the same schedule as the memory-to-Notes backup (hourly). Merge is by summing counters,
  incrementally: read the note, add the local delta, write the note, take its totals as the new local
  totals, zero the delta. Every machine of the account adds its own delta, so all machines converge on
  the same table. (Two machines writing at the same instant can lose one delta; it is not retried.)

## Steps

1. The four microagents (`app/microagents/`), each with schema + code verify, on the ladder.
2. Engine: tags + advice before the first tier; advisor on every stay-retry; recording at turn end;
   logs for every decision.
3. Stats store + Notes sync on the hourly schedule.
4. Check on the test box: E2 (mailbox) -- a question-instead-of-action attempt gets "email tools /
   request_info" from the advisor and closes on the same tier; stats file and note fill in.

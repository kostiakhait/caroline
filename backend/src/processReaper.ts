/**
 * Background cleanup for a real, confirmed leak (2026-09-06): a query()'s
 * underlying CLI process (and its whole tree of MCP-server child processes)
 * can simply fail to exit when Caroline gives up on it and moves to a fresh
 * query() -- not the MCP servers themselves dying and leaving orphans, but
 * their PARENT (the CLI process) just never terminating. Found live: 11 such
 * stuck process trees, the oldest from the previous afternoon, together
 * holding 240+ leaked node.exe processes, none of which Restart Manager
 * or any existing cleanup path had a reason to ever look at again.
 *
 * Approach, per explicit instruction (2026-09-06): timeout-based, not
 * signal-based -- there's no reliable "did this actually die" event to hook,
 * so a session's caller (server.ts) records which PID was its own CLI
 * process right before abandoning it, and this module force-kills that PID
 * (and its whole subtree) if it's STILL alive after a long grace period.
 * 5 minutes (the caller's choice) is long enough that a session merely
 * taking a while to wind down naturally is never mistaken for one that's
 * actually stuck -- this only ever fires on the case that's already
 * confirmed to happen: never exiting at all.
 *
 * Per a SEPARATE, later explicit instruction (also 2026-09-06): every system
 * call here goes through WinAPI directly, never by spawning an external
 * process (no PowerShell, no taskkill). The first version of this file did
 * exactly that via PowerShell/Get-CimInstance -- confirmed live the same day
 * as both slow (300-500ms per call, on every single query() creation) and
 * fragile (a quoting bug silently broke it entirely for its whole time in
 * production). The actual WinAPI work (kernel32.dll's Toolhelp32Snapshot for
 * enumeration, Process.Kill()'s TerminateProcess for killing) now lives in
 * the WPF shell's ProcessTreeHelper.cs, reached over AppBrowserHost's
 * existing local HTTP bridge (port 8767) -- that's a request to an already-
 * running sibling process over a socket, not a new process being spawned.
 */

const APP_BROWSER_HOST = "http://127.0.0.1:8767";

interface ProcessEntry {
  pid: number;
  parentPid: number;
  name: string;
}

async function listAllProcesses(): Promise<ProcessEntry[]> {
  const res = await fetch(`${APP_BROWSER_HOST}/process_list`, { signal: AbortSignal.timeout(10_000) });
  if (!res.ok) throw new Error(`/process_list returned ${res.status}`);
  return (await res.json()) as ProcessEntry[];
}

/** Every OS process descended from rootPid, recursively (BFS over parentPid),
 *  not just its direct children -- diagnostic-only (see server.ts's call
 *  sites), so every restart trigger logs a concrete number instead of
 *  leaving "did the process tree leak" to be reconstructed by hand later.
 *  Returns -1 on any failure rather than throwing -- never allowed to be the
 *  reason a restart itself fails. */
export async function countDescendantProcesses(rootPid: number): Promise<number> {
  try {
    const procs = await listAllProcesses();
    const ids = new Set<number>([rootPid]);
    let frontier = [rootPid];
    while (frontier.length > 0) {
      const children = procs.filter((p) => frontier.includes(p.parentPid)).map((p) => p.pid);
      const fresh = children.filter((pid) => !ids.has(pid));
      for (const pid of fresh) ids.add(pid);
      frontier = fresh;
    }
    return ids.size - 1; // exclude the root itself, count descendants only
  } catch (err) {
    console.error("[caroline] countDescendantProcesses failed (ignored):", err);
    return -1;
  }
}

/** PIDs of the current DIRECT children of rootPid only (not the full recursive
 *  descendant tree, unlike countDescendantProcesses in server.ts) -- each
 *  query() call spawns exactly one such child (the CLI process itself), so
 *  diffing two snapshots of this set around a query() call isolates that
 *  one new PID without needing anything the SDK doesn't expose. */
export async function snapshotDirectChildPids(rootPid: number): Promise<number[]> {
  try {
    const procs = await listAllProcesses();
    return procs.filter((p) => p.parentPid === rootPid).map((p) => p.pid);
  } catch (err) {
    console.error("[caroline] [reaper] snapshotDirectChildPids failed (ignored):", err);
    return [];
  }
}

/** Force-kills pid and its entire process tree if (and only if) it's still
 *  alive after graceMs -- fire-and-forget, never awaited by the caller.
 *  /kill_process itself reports wasAlive so this can log which case it was,
 *  unlike the old taskkill-based version which couldn't tell "already gone"
 *  apart from "the kill itself failed" at all. */
export function scheduleReapIfStale(pid: number, graceMs: number): void {
  console.error(`[caroline] [reaper] watching pid=${pid} from an abandoned session -- will force-kill its tree if still alive in ${Math.round(graceMs / 1000)}s`);
  setTimeout(async () => {
    try {
      const res = await fetch(`${APP_BROWSER_HOST}/kill_process`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pid }),
        signal: AbortSignal.timeout(10_000),
      });
      const result = (await res.json()) as { ok: boolean; wasAlive?: boolean; error?: string };
      if (result.wasAlive) {
        console.error(`[caroline] [reaper] pid=${pid} was still alive after the grace period -- force-killed its whole tree`);
      } else if (result.ok) {
        console.error(`[caroline] [reaper] pid=${pid} had already exited on its own -- nothing to do`);
      } else {
        console.error(`[caroline] [reaper] pid=${pid} kill_process reported failure: ${result.error}`);
      }
    } catch (err) {
      console.error(`[caroline] [reaper] kill_process pid=${pid} failed (ignored):`, err);
    }
  }, graceMs);
}

/** Diffs two direct-child-pid snapshots (taken shortly before and shortly
 *  after a query() call) to find the one PID that's new in `after` --
 *  that's this session's own CLI process. Returns null if none or more than
 *  one appeared (another tab's own query() racing in the same instant, or
 *  the child hadn't spawned yet by the time `after` was taken) -- best-effort
 *  diagnostic, matching countDescendantProcesses' own tolerance for "-1
 *  means unknown" rather than guessing wrong.
 */
export function findNewPid(before: number[], after: number[]): number | null {
  const beforeSet = new Set(before);
  const added = after.filter((p) => !beforeSet.has(p));
  return added.length === 1 ? added[0] : null;
}

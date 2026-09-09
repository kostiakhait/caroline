using System.Net.Http;
using System.Text.Json;
using System.Threading;

namespace Caroline.Native;

/// <summary>
/// Polls the backend's own GET /api/status from OUTSIDE its process,
/// independent of any timer/watchdog running inside the Node event loop
/// (server.ts's own checkHang()). This exists specifically for the failure
/// mode that internal watchdog can never catch: the Node process staying
/// alive but its event loop freezing solid, so nothing inside it -- not
/// checkHang's setInterval, not the reminder scheduler, nothing -- ever
/// fires again either. Confirmed live on 2026-08-30/31: a session hung
/// repeatedly for 3.5 hours (checkHang's own "session appears hung" firing
/// 8 separate times), then the whole process went silent -- no more log
/// lines of ANY kind, including the hourly reminder delivery that had been
/// reliable all day -- meaning the event loop itself, not just one stuck
/// turn, had stopped running entirely.
///
/// Per explicit instruction (2026-09-08): this class used to check ONLY the
/// primary tab's own turnPending/lastActivityMs (one representative signal
/// for "is the backend frozen") and, on trouble, always killed the WHOLE
/// backend process -- which took every other, perfectly healthy tab down
/// with it. Confirmed live the same day: tab=2 hung repeatedly for 16
/// minutes while tab=3 kept working fine, and the eventual whole-process
/// restart (triggered because tab=1/primary ALSO degraded from the same
/// shared-process resource contention) killed tab=3's own in-flight work
/// too. Now split into two genuinely separate roles, matching the fact that
/// "the process is completely unreachable" really is whole-process (there's
/// no tab to blame it on), while "one tab's own turn is stuck" is not:
///   - TabId == null: a single, shared instance that ONLY ever reacts to
///     the HTTP call itself failing (connection refused/timeout/non-2xx) --
///     the one case with no per-tab meaning at all. Fires Frozen; caller's
///     only remedy is the same whole-process RestartBackend as before.
///   - TabId != null: one instance PER OPEN TAB, created/disposed alongside
///     that tab's own lifecycle (see MainWindow's AddTabAsync/CloseTabAsync).
///     Only ever reacts to a SUCCESSFUL response showing THAT tab's own
///     entry in /api/status's `tabs[]` array stuck past StuckTurnMs -- never
///     to a connectivity failure (ambiguous for a single tab, left entirely
///     to the TabId==null instance). Fires TabFrozen(tabId, reason); the
///     caller's remedy is to kill just that tab's own cliProcessPid (see
///     ChatSession.getStatus()), never the whole backend.
///
/// This class only detects; it doesn't touch anything else. Actually
/// recovering is the caller's job (see MainWindow's Frozen/TabFrozen
/// handlers), matching BackendProcess's own Crashed event shape.
/// </summary>
public sealed class BackendHealthWatchdog : IDisposable
{
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(10) };
    private readonly System.Threading.Timer _timer;
    private readonly int _port;
    private readonly string? _tabId;
    private int _consecutiveBadChecks;

    // Two consecutive failed checks (~60s apart -> ~60-90s of real
    // unresponsiveness) before acting -- avoids reacting to one transient
    // blip (e.g. the machine briefly thrashing under unrelated load) while
    // still recovering far faster than "however long a human happens to
    // notice", which was the actual failure mode this replaces.
    private const int BadChecksBeforeAction = 2;
    private static readonly TimeSpan CheckInterval = TimeSpan.FromSeconds(30);
    // Root-caused live on 2026-08-31: with no grace period, this watchdog
    // declared the backend "frozen" (2 bad checks = ~60s of connection
    // refused) and killed it while it was still legitimately starting up
    // under heavy system load (confirmed in caroline.log: Process.Start()
    // and even Kill() itself sometimes took several seconds each on their
    // own that day) -- producing an exact-clockwork restart every 60s
    // forever, killing each new backend before it ever got a chance to
    // finish coming up. This grace period suppresses ACTING on bad checks
    // (they're still logged) for a while after every (re)start.
    //
    // 3 minutes was still not enough under this same day's exceptionally
    // heavy load (confirmed live: zero output at all from the backend's own
    // stdout/stderr -- not even its first "backend listening" log line --
    // across multiple full 3-minute grace windows in a row, meaning it was
    // stuck very early, most likely in ensureWorkspace()'s `claude mcp add`
    // subprocess spawns, which are exactly the kind of operation this
    // machine's other confirmed symptoms that same day -- `tasklist` and
    // `dotnet build` both taking minutes instead of seconds -- would also
    // slow to a crawl). 10 minutes gives real headroom for that; a healthy
    // machine still recovers in well under this, a broken backend still
    // eventually gets killed, just not as trigger-happily as before.
    private static readonly TimeSpan StartupGraceWindow = TimeSpan.FromMinutes(10);
    private DateTime _graceUntilUtc = DateTime.UtcNow + StartupGraceWindow;

    /// <summary>Caller must invoke this right after starting/restarting the
    /// backend, so a fresh grace window applies to the fresh process -- not
    /// just once, at this watchdog's own construction time.</summary>
    public void NotifyBackendRestarted()
    {
        _graceUntilUtc = DateTime.UtcNow + StartupGraceWindow;
        _consecutiveBadChecks = 0;
        LogLine?.Invoke($"[health-watchdog] NotifyBackendRestarted: grace window extended to {_graceUntilUtc:O}");
    }
    // If /api/status DOES respond but reports a turn that's been pending
    // this long, the backend's OWN watchdog (checkHang, HANG_TIMEOUT_MS +
    // escalation grace, now also hangCount's fast-path -- see server.ts) has
    // already had several times its own worst-case recovery window to fix
    // this itself. Still stuck past this point means something is wrong
    // that only an external kill can fix, not "give it a bit longer".
    private const long StuckTurnMs = 5 * 60_000;

    /// <summary>Fired (on a background thread pool thread, not the UI thread --
    /// caller must Dispatcher.Invoke) once BadChecksBeforeAction consecutive
    /// checks all indicate the backend is completely unreachable. Only ever
    /// fired by a TabId == null (whole-process) instance -- see this class's
    /// own doc comment.</summary>
    public event Action<string>? Frozen;

    /// <summary>Same idea as Frozen, but scoped to ONE tab: fired only by a
    /// TabId != null instance, only when /api/status responds fine but THAT
    /// tab's own entry shows a turn stuck past StuckTurnMs. Args are
    /// (tabId, reason) -- caller's remedy is a tab-scoped kill, never
    /// RestartBackend.</summary>
    public event Action<string, string>? TabFrozen;

    /// <summary>Every check's outcome, healthy or not (same shape as
    /// BackendProcess.OutputLine) -- routed through MainWindow to Logger.Log
    /// so caroline.log has a full, continuous record of this watchdog's own
    /// view of backend health, not just the moment it finally acts. Fired on
    /// a background thread pool thread, same as Frozen.</summary>
    public event Action<string>? LogLine;

    /// <param name="tabId">null for the single, shared whole-process
    /// (connectivity-only) instance; a specific tab id for a per-tab
    /// instance watching only that tab's own /api/status entry.</param>
    public BackendHealthWatchdog(int port, string? tabId = null)
    {
        _port = port;
        _tabId = tabId;
        _timer = new System.Threading.Timer(_ => Tick(), null, CheckInterval, CheckInterval);
    }

    private int _tickCount;

    private async void Tick()
    {
        var tickNum = ++_tickCount;
        LogLine?.Invoke($"[health-watchdog] tick #{tickNum} starting (thread={Environment.CurrentManagedThreadId})");
        string? badReason = await CheckOnceAsync();
        if (badReason == null)
        {
            if (_consecutiveBadChecks > 0)
                LogLine?.Invoke($"[health-watchdog] tick #{tickNum}: healthy, recovered after {_consecutiveBadChecks} bad check(s)");
            else
                LogLine?.Invoke($"[health-watchdog] tick #{tickNum}: healthy");
            _consecutiveBadChecks = 0;
            return;
        }

        _consecutiveBadChecks++;
        var inGrace = DateTime.UtcNow < _graceUntilUtc;
        LogLine?.Invoke($"[health-watchdog] tick #{tickNum}: bad check {_consecutiveBadChecks}/{BadChecksBeforeAction}: {badReason}" +
            (inGrace ? $" (within startup grace window, {(_graceUntilUtc - DateTime.UtcNow).TotalSeconds:F0}s left -- not acting on this)" : ""));
        if (inGrace) return;
        if (_consecutiveBadChecks < BadChecksBeforeAction) return;

        _consecutiveBadChecks = 0; // don't fire again next tick for the same episode
        if (_tabId == null)
        {
            LogLine?.Invoke($"[health-watchdog] tick #{tickNum}: {BadChecksBeforeAction} consecutive bad checks -- declaring the backend frozen: {badReason}. Invoking Frozen (subscriber count unknown from here, see MainWindow's own log line right after this one).");
            try
            {
                Frozen?.Invoke(badReason);
                LogLine?.Invoke($"[health-watchdog] tick #{tickNum}: Frozen?.Invoke() returned normally");
            }
            catch (Exception ex)
            {
                LogLine?.Invoke($"[health-watchdog] tick #{tickNum}: Frozen?.Invoke() THREW: {ex}");
            }
        }
        else
        {
            LogLine?.Invoke($"[health-watchdog] tick #{tickNum} (tab={_tabId}): {BadChecksBeforeAction} consecutive bad checks -- declaring tab {_tabId} frozen: {badReason}. Invoking TabFrozen.");
            try
            {
                TabFrozen?.Invoke(_tabId, badReason);
                LogLine?.Invoke($"[health-watchdog] tick #{tickNum} (tab={_tabId}): TabFrozen?.Invoke() returned normally");
            }
            catch (Exception ex)
            {
                LogLine?.Invoke($"[health-watchdog] tick #{tickNum} (tab={_tabId}): TabFrozen?.Invoke() THREW: {ex}");
            }
        }
    }

    /// <summary>Returns null if healthy, else a human-readable reason it isn't.
    /// A TabId == null instance only ever evaluates raw HTTP reachability
    /// (never any tab's content -- see this class's own doc comment); a
    /// TabId != null instance only ever evaluates ITS OWN tab's entry in a
    /// SUCCESSFUL response's `tabs[]` array, never connectivity (ambiguous
    /// for a single tab -- left entirely to the TabId==null instance, which
    /// runs concurrently and already covers that case).</summary>
    private async Task<string?> CheckOnceAsync()
    {
        JsonDocument doc;
        try
        {
            using var resp = await _http.GetAsync($"http://127.0.0.1:{_port}/api/status");
            if (!resp.IsSuccessStatusCode)
            {
                return _tabId == null ? $"HTTP {(int)resp.StatusCode} from /api/status" : null;
            }
            var body = await resp.Content.ReadAsStringAsync();
            doc = JsonDocument.Parse(body);
        }
        catch (Exception ex)
        {
            // Connection refused, timeout, malformed response -- all mean
            // "can't get a healthy answer right now". Only actionable by the
            // whole-process (TabId == null) instance -- a single tab's own
            // instance has no way to tell "the whole thing is down" apart
            // from "just this tick was unlucky for some unrelated reason",
            // and acting on it here would be redundant with (and racing)
            // the shared instance that already owns this case.
            return _tabId == null ? $"/api/status did not respond: {ex.Message}" : null;
        }

        using (doc)
        {
            var root = doc.RootElement;
            if (_tabId == null) return null; // this instance only ever reacts to unreachability, checked above

            JsonElement? tabEntry = null;
            if (root.TryGetProperty("tabs", out var tabs) && tabs.ValueKind == JsonValueKind.Array)
            {
                foreach (var t in tabs.EnumerateArray())
                {
                    if (t.TryGetProperty("tabId", out var id) && id.GetString() == _tabId) { tabEntry = t; break; }
                }
            }
            // Tab not present (closed backend-side, or a momentary gap right
            // after a fresh backend process comes up before it's re-opened
            // its saved tabs) -- nothing to check yet, not a bad check.
            if (tabEntry == null) return null;

            var turnPending = tabEntry.Value.TryGetProperty("turnPending", out var tp) && tp.GetBoolean();
            var lastActivityMs = tabEntry.Value.TryGetProperty("lastActivityMs", out var la) ? la.GetInt64() : 0;

            if (turnPending && lastActivityMs > StuckTurnMs)
            {
                return $"Tab {_tabId}'s conversation turn has been stuck for {lastActivityMs / 1000}s with no recovery from the backend's own watchdog.";
            }
            return null;
        }
    }

    public void Dispose()
    {
        _timer.Dispose();
        _http.Dispose();
    }
}

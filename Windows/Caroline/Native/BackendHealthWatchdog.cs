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
/// This class only detects; it doesn't touch anything else. Actually
/// recovering (kill + restart the backend, show a message box) is the
/// caller's job (see MainWindow's Frozen handler), matching BackendProcess's
/// own Crashed event shape.
/// </summary>
public sealed class BackendHealthWatchdog : IDisposable
{
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(10) };
    private readonly System.Threading.Timer _timer;
    private readonly int _port;
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
    /// checks all indicate the backend is unresponsive or irrecoverably stuck.</summary>
    public event Action<string>? Frozen;

    /// <summary>Every check's outcome, healthy or not (same shape as
    /// BackendProcess.OutputLine) -- routed through MainWindow to Logger.Log
    /// so caroline.log has a full, continuous record of this watchdog's own
    /// view of backend health, not just the moment it finally acts. Fired on
    /// a background thread pool thread, same as Frozen.</summary>
    public event Action<string>? LogLine;

    public BackendHealthWatchdog(int port)
    {
        _port = port;
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

    /// <summary>Returns null if healthy, else a human-readable reason it isn't.</summary>
    private async Task<string?> CheckOnceAsync()
    {
        try
        {
            using var resp = await _http.GetAsync($"http://127.0.0.1:{_port}/api/status");
            if (!resp.IsSuccessStatusCode) return $"HTTP {(int)resp.StatusCode} from /api/status";

            var body = await resp.Content.ReadAsStringAsync();
            using var doc = JsonDocument.Parse(body);
            var root = doc.RootElement;

            var turnPending = root.TryGetProperty("turnPending", out var tp) && tp.GetBoolean();
            var lastActivityMs = root.TryGetProperty("lastActivityMs", out var la) ? la.GetInt64() : 0;

            if (turnPending && lastActivityMs > StuckTurnMs)
            {
                return $"A conversation turn has been stuck for {lastActivityMs / 1000}s with no recovery from the backend's own watchdog.";
            }
            return null;
        }
        catch (Exception ex)
        {
            // Connection refused, timeout, malformed response -- all mean
            // "can't get a healthy answer right now", which is exactly what
            // this class exists to detect. The specific exception type
            // doesn't change what we do about it.
            return $"/api/status did not respond: {ex.Message}";
        }
    }

    public void Dispose()
    {
        _timer.Dispose();
        _http.Dispose();
    }
}

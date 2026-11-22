using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Text.Json;
using Caroline.Services;
using Microsoft.Win32;

namespace Caroline.Native;

/// <summary>
/// Watchdog for the Caroline shell (Caroline.exe), modelled on the Windows
/// client of Tunnel Cat (shortnerdcat.exe --watchdog): the same exe runs in
/// two roles, and each keeps the other alive.
///
///   Caroline.exe              the shell: WPF windows, tray, supervisor -> backend
///   Caroline.exe --watchdog   no windows; starts/attaches to the shell and
///                             restarts it when it stops without having asked to
///
/// Why: the backend was already supervised (backend-py/supervisor.py), but the
/// shell itself was not -- on 2026-10-28 and 2026-10-30 Caroline.exe vanished
/// without a trace and stayed gone, and SupervisorProcessGuard's kill-on-close
/// job took the whole tree down with it.
///
/// Contract between the two (all names are per Windows session):
///   - mutex  Local\Caroline.Watchdog.Running   held by the watchdog; the shell
///     starts a watchdog if it cannot open it (checked at start and every 30 s)
///   - event  Local\Caroline.Watchdog.CleanShutdown   set by the shell before an
///     exit somebody asked for (tray Exit, update, installer, logoff). Without
///     it any exit -- crash, kill, even exit code 0 -- is "unclean": journaled
///     in crashes.jsonl and answered with a restart
///   - pipe   Caroline.Watchdog.{session}   the shell says hello (its PID, taken
///     from the live connection, never from a remembered number: a recycled PID
///     once kept a Tunnel Cat watchdog attached to strangers for 49 hours) and
///     then sends a heartbeat every 5 s -- but only while its UI thread answers,
///     so a frozen window stops the heartbeat and the watchdog kills + restarts
///   - crash loop: three lifetimes under 10 s in a row -> the watchdog stops
///     restarting and tells the user once, instead of looping forever
/// </summary>
public sealed record WatchdogNames(string Mutex, string CleanEvent, string Pipe)
{
    public static WatchdogNames Default { get; } = new(
        @"Local\Caroline.Watchdog.Running",
        @"Local\Caroline.Watchdog.CleanShutdown",
        $"Caroline.Watchdog.{Process.GetCurrentProcess().SessionId}");
}

// ---------------------------------------------------------------------------
// Watchdog side: receives hello/heartbeat from the shell
// ---------------------------------------------------------------------------

public sealed class WatchdogPipeServer : IDisposable
{
    private readonly string _name;
    private readonly CancellationTokenSource _cts = new();
    private int _pid;
    private long _lastBeat;           // Stopwatch timestamp
    private volatile bool _connected;

    public WatchdogPipeServer(string name)
    {
        _name = name;
        _ = Task.Run(AcceptLoop);
    }

    /// <summary>PID the connected shell reported in its hello, or 0.</summary>
    public int MainPid => _connected ? Volatile.Read(ref _pid) : 0;
    public bool Connected => _connected;
    public TimeSpan BeatAge => Stopwatch.GetElapsedTime(Interlocked.Read(ref _lastBeat));

    /// <summary>Forget the dead shell's connection state before a new one starts.</summary>
    public void Reset()
    {
        Interlocked.Exchange(ref _pid, 0);
        Touch();
    }

    /// <summary>Count "now" as a heartbeat (after resume from sleep).</summary>
    public void Touch() => Interlocked.Exchange(ref _lastBeat, Stopwatch.GetTimestamp());

    private async Task AcceptLoop()
    {
        while (!_cts.IsCancellationRequested)
        {
            try
            {
                using var server = new NamedPipeServerStream(_name, PipeDirection.In, 1,
                    PipeTransmissionMode.Byte, PipeOptions.Asynchronous);
                await server.WaitForConnectionAsync(_cts.Token);
                _connected = true;
                Touch();
                using var reader = new StreamReader(server);
                string? line;
                while ((line = await reader.ReadLineAsync(_cts.Token)) != null)
                {
                    Touch();
                    try
                    {
                        using var doc = JsonDocument.Parse(line);
                        if (doc.RootElement.TryGetProperty("t", out var t) && t.GetString() == "hello"
                            && doc.RootElement.TryGetProperty("pid", out var pid))
                            Interlocked.Exchange(ref _pid, pid.GetInt32());
                    }
                    catch (JsonException) { /* a garbled line still proves the shell is talking */ }
                }
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex)
            {
                Logger.Log($"[watchdog] pipe server: {ex.Message}");
                try { await Task.Delay(500, _cts.Token); } catch (OperationCanceledException) { break; }
            }
            finally { _connected = false; }
        }
    }

    public void Dispose() => _cts.Cancel();
}

// ---------------------------------------------------------------------------
// Shell side
// ---------------------------------------------------------------------------

public static class WatchdogClient
{
    private static readonly TimeSpan BeatEvery = TimeSpan.FromSeconds(5);
    private static readonly TimeSpan UiMustAnswerWithin = TimeSpan.FromSeconds(20);
    private static readonly TimeSpan EnsureEvery = TimeSpan.FromSeconds(30);

    private static long _uiBeat = Stopwatch.GetTimestamp();
    private static volatile bool _stopping;
    private static System.Windows.Threading.DispatcherTimer? _uiTimer;
    private static System.Threading.Timer? _ensureTimer;

    /// <summary>Starts the watchdog if none runs, keeps checking that one does,
    /// and keeps the heartbeat going. Call once the shell is the single instance.</summary>
    public static void Start(WatchdogNames? names = null)
    {
        names ??= WatchdogNames.Default;
        EnsureRunning(names);
        _ensureTimer = new System.Threading.Timer(_ => { if (!_stopping) EnsureRunning(names); }, null, EnsureEvery, EnsureEvery);

        // Ticks on the UI thread: if that thread hangs, the heartbeat below stops.
        _uiTimer = new System.Windows.Threading.DispatcherTimer { Interval = BeatEvery };
        _uiTimer.Tick += (_, _) => Interlocked.Exchange(ref _uiBeat, Stopwatch.GetTimestamp());
        _uiTimer.Start();

        _ = Task.Run(() => HeartbeatLoop(names));
    }

    public static bool WatchdogRunning(WatchdogNames names)
    {
        try
        {
            if (!Mutex.TryOpenExisting(names.Mutex, out var m)) return false;
            m.Dispose();
            return true;
        }
        catch { return false; }
    }

    private static void EnsureRunning(WatchdogNames names)
    {
        if (WatchdogRunning(names)) return;
        try
        {
            var exe = Environment.ProcessPath!;
            Process.Start(new ProcessStartInfo(exe, "--watchdog")
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                WorkingDirectory = Path.GetDirectoryName(exe)!,
            })?.Dispose();
            Logger.Log("[WatchdogClient] no watchdog was running -- started one");
        }
        catch (Exception ex) { Logger.Log($"[WatchdogClient] could not start the watchdog: {ex.Message}"); }
    }

    private static async Task HeartbeatLoop(WatchdogNames names)
    {
        while (!_stopping)
        {
            try
            {
                using var client = new NamedPipeClientStream(".", names.Pipe, PipeDirection.Out);
                await client.ConnectAsync(2000);
                using var writer = new StreamWriter(client) { AutoFlush = true };
                await writer.WriteLineAsync($"{{\"t\":\"hello\",\"pid\":{Environment.ProcessId}}}");
                while (!_stopping)
                {
                    await Task.Delay(BeatEvery);
                    if (Stopwatch.GetElapsedTime(Interlocked.Read(ref _uiBeat)) < UiMustAnswerWithin)
                        await writer.WriteLineAsync("{\"t\":\"hb\"}");
                }
            }
            catch { /* no watchdog yet, or it went away: EnsureRunning brings one, we reconnect */ }
            if (!_stopping) await Task.Delay(BeatEvery);
        }
    }

    /// <summary>Tell the watchdog the coming exit is wanted. Must run before the
    /// process exits (it is a kernel event, so there is nothing to flush).</summary>
    public static void SignalCleanShutdown(WatchdogNames? names = null)
    {
        _stopping = true;
        try
        {
            if (EventWaitHandle.TryOpenExisting((names ?? WatchdogNames.Default).CleanEvent, out var ev))
            {
                ev.Set();
                ev.Dispose();
            }
        }
        catch (Exception ex) { Logger.Log($"[WatchdogClient] could not signal clean shutdown: {ex.Message}"); }
    }
}

// ---------------------------------------------------------------------------
// The watchdog process
// ---------------------------------------------------------------------------

public sealed class WatchdogOptions
{
    public WatchdogNames Names { get; init; } = WatchdogNames.Default;
    /// <summary>Starts the shell. The flag says it is a restart after an unclean exit.</summary>
    public required Func<bool, Process?> LaunchMain { get; init; }
    /// <summary>Full path of the shell's exe; only a process with this image counts as the shell.</summary>
    public required string MainImagePath { get; init; }
    public Action<string>? OnGiveUp { get; init; }

    public TimeSpan Tick { get; init; } = TimeSpan.FromSeconds(1);
    public TimeSpan RestartDelay { get; init; } = TimeSpan.FromSeconds(3);
    public TimeSpan FastFail { get; init; } = TimeSpan.FromSeconds(10);
    public int MaxFastFails { get; init; } = 3;
    public TimeSpan HangWindow { get; init; } = TimeSpan.FromSeconds(90);
    public TimeSpan HangRecheck { get; init; } = TimeSpan.FromSeconds(15);
    public TimeSpan CleanShutdownDeadline { get; init; } = TimeSpan.FromSeconds(60);
    public TimeSpan ResumeGrace { get; init; } = TimeSpan.FromSeconds(60);
}

public static class WatchdogRunner
{
    /// <summary>Returns when the shell exited cleanly (0), the watchdog is a
    /// duplicate (0), or it gave up (nonzero).</summary>
    public static async Task<int> RunAsync(WatchdogOptions o)
    {
        using var mutex = new Mutex(true, o.Names.Mutex, out bool first);
        if (!first) return 0;   // another watchdog already runs

        using var cleanEvent = new EventWaitHandle(false, EventResetMode.ManualReset, o.Names.CleanEvent);
        using var pipe = new WatchdogPipeServer(o.Names.Pipe);
        Logger.Log($"[watchdog] started, pid={Environment.ProcessId}");

        DateTime graceUntil = DateTime.MinValue;
        PowerModeChangedEventHandler onPower = (_, e) =>
        {
            if (e.Mode != PowerModes.Resume) return;
            graceUntil = DateTime.Now + o.ResumeGrace;
            pipe.Touch();
            Logger.Log("[watchdog] system resumed -- heartbeat check paused for a minute");
        };
        SystemEvents.PowerModeChanged += onPower;
        try
        {
            var main = await AttachExisting(o, pipe) ?? o.LaunchMain(false);
            if (main == null) { Logger.Log("[watchdog] could not start the shell -- giving up"); return 3; }
            var startedAt = SafeStartTime(main);
            var exitHandle = OpenForExitCode(main.Id);
            int fastFails = 0;

            while (true)
            {
                bool cleanSeen = false;
                DateTime cleanDeadline = DateTime.MaxValue;
                string? killReason = null;
                double? lastBeatAgo = null;

                while (!main.WaitForExit(o.Tick))
                {
                    if (!cleanSeen && cleanEvent.WaitOne(0))
                    {
                        cleanSeen = true;
                        cleanDeadline = DateTime.Now + o.CleanShutdownDeadline;
                        Logger.Log("[watchdog] shell announced a clean shutdown");
                    }
                    if (cleanSeen)
                    {
                        if (DateTime.Now > cleanDeadline)
                        {
                            killReason = "announced a clean shutdown but did not exit in time";
                            Kill(main);
                        }
                        continue;
                    }
                    if (killReason != null || !pipe.Connected || DateTime.Now < graceUntil || pipe.BeatAge <= o.HangWindow) continue;

                    // No heartbeat for a long time. Wait and look again before killing: after sleep the
                    // shell's own timer may simply not have fired yet.
                    Logger.Log($"[watchdog] no heartbeat for {pipe.BeatAge.TotalSeconds:F0}s -- looking again in {o.HangRecheck.TotalSeconds:F0}s");
                    await Task.Delay(o.HangRecheck);
                    if (main.HasExited || cleanEvent.WaitOne(0) || DateTime.Now < graceUntil || pipe.BeatAge <= o.HangWindow) continue;
                    lastBeatAgo = pipe.BeatAge.TotalSeconds;
                    killReason = $"frozen: no UI heartbeat for {lastBeatAgo:F0}s, killed by the watchdog";
                    Logger.Log($"[watchdog] {killReason}");
                    Kill(main);
                }

                long? exitCode = ExitCodeOf(main, exitHandle);
                if (exitHandle != 0) CloseHandle(exitHandle);
                int pid = main.Id;
                double uptime = startedAt.HasValue ? (DateTime.Now - startedAt.Value).TotalSeconds : 0;
                main.Dispose();

                if (cleanSeen || cleanEvent.WaitOne(0))
                {
                    Logger.Log($"[watchdog] shell exited cleanly (code {exitCode}) -- watchdog exits too");
                    return 0;
                }

                // Unclean: nobody asked for this.
                var reason = killReason ?? "exited without announcing it (crash, kill, or an exit nobody explained)";
                Logger.Log($"[watchdog] shell pid={pid} stopped after {uptime:F0}s, exit code {exitCode?.ToString() ?? "?"}: {reason}");
                CrashJournal.Append(new CrashJournal.Entry(DateTime.Now, "watchdog", reason, pid, startedAt, Math.Round(uptime, 1),
                    exitCode, exitCode.HasValue ? CrashJournal.ExitCodeName(exitCode.Value) : null, lastBeatAgo, CrashJournal.TailOfLog()));

                fastFails = uptime < o.FastFail.TotalSeconds ? fastFails + 1 : 0;
                if (fastFails >= o.MaxFastFails)
                {
                    var msg = $"Caroline stopped {fastFails} times in a row right after starting, so it will not be restarted again.\n\nSee {Logger.LogPath} and {CrashJournal.JournalPath}.";
                    Logger.Log($"[watchdog] crash loop ({fastFails} fast failures) -- giving up");
                    o.OnGiveUp?.Invoke(msg);
                    return 2;
                }

                await Task.Delay(o.RestartDelay);
                pipe.Reset();
                // A new shell may have been started meanwhile (the user clicked the icon): take that one.
                main = await AttachExisting(o, pipe, wait: false) ?? o.LaunchMain(true);
                if (main == null)
                {
                    await Task.Delay(TimeSpan.FromSeconds(10));
                    main = await AttachExisting(o, pipe, wait: false) ?? o.LaunchMain(true);
                }
                if (main == null) { Logger.Log("[watchdog] could not restart the shell -- giving up"); return 3; }
                startedAt = SafeStartTime(main);
                exitHandle = OpenForExitCode(main.Id);
                Logger.Log($"[watchdog] shell running as pid={main.Id}");
            }
        }
        finally { SystemEvents.PowerModeChanged -= onPower; }
    }

    private static DateTime? SafeStartTime(Process p)
    {
        try { return p.StartTime; } catch { return DateTime.Now; }
    }

    // .NET's Process.ExitCode throws for a process this one did not start (the shell that launched
    // the watchdog). So a handle is opened while the shell is alive and read with GetExitCodeProcess.
    [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
    private static extern nint OpenProcess(uint access, bool inherit, int pid);
    [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetExitCodeProcess(nint h, out uint code);
    [System.Runtime.InteropServices.DllImport("kernel32.dll")]
    private static extern bool CloseHandle(nint h);

    private static nint OpenForExitCode(int pid) => OpenProcess(0x1000 /* QUERY_LIMITED_INFORMATION */, false, pid);

    private static long? ExitCodeOf(Process p, nint handle)
    {
        try { return p.ExitCode; } catch (InvalidOperationException) { /* not our child */ }
        if (handle != 0 && GetExitCodeProcess(handle, out var code) && code != 259 /* STILL_ACTIVE */) return (int)code;
        return null;
    }

    private static void Kill(Process p)
    {
        try { p.Kill(entireProcessTree: true); } catch { /* already gone */ }
    }

    /// <summary>A shell that is already running (started by the user before the
    /// watchdog was). Its PID comes from the live pipe connection, and only a
    /// process whose image is the shell's exe is accepted.</summary>
    private static async Task<Process?> AttachExisting(WatchdogOptions o, WatchdogPipeServer pipe, bool wait = true)
    {
        int pid = pipe.MainPid;
        for (var until = DateTime.Now.AddSeconds(wait ? 2 : 0); pid == 0 && DateTime.Now < until;)
        {
            await Task.Delay(100);
            pid = pipe.MainPid;
        }
        if (pid != 0 && IsShell(o, pid, out var p)) { Logger.Log($"[watchdog] attached to the running shell pid={pid}"); return p; }

        // No hello yet (an older shell, or one that has not connected): look by image.
        foreach (var cand in Process.GetProcessesByName(Path.GetFileNameWithoutExtension(o.MainImagePath)))
        {
            if (cand.Id != Environment.ProcessId && SameImage(cand, o.MainImagePath))
            {
                Logger.Log($"[watchdog] attached to the running shell pid={cand.Id}");
                return cand;
            }
            cand.Dispose();
        }
        return null;
    }

    private static bool IsShell(WatchdogOptions o, int pid, out Process? p)
    {
        p = null;
        try
        {
            var cand = Process.GetProcessById(pid);
            if (pid != Environment.ProcessId && SameImage(cand, o.MainImagePath)) { p = cand; return true; }
            cand.Dispose();
        }
        catch { }
        return false;
    }

    private static bool SameImage(Process p, string path)
    {
        try { return string.Equals(p.MainModule?.FileName, path, StringComparison.OrdinalIgnoreCase); }
        catch { return false; }
    }
}

using System.Diagnostics;
using System.IO;
using System.Text.Json;
using Caroline.Services;

namespace Caroline.Native;

/// <summary>
/// Record of every time the Caroline shell stopped without anyone asking it to
/// (incident 2026-10-30 19:59: caroline.log simply stopped, no exception, no
/// "exiting" line, no Windows crash event -- an outside kill, and nothing
/// could say so afterwards). Two writers feed %LocalAppData%\Caroline\crashes.jsonl:
///
/// - the watchdog process (WatchdogRunner), which sees the exit code and knows
///   whether it had asked for the exit or killed a frozen shell itself;
/// - the shell at startup (<see cref="MarkStarted"/>), for the case where no
///   watchdog was alive to see the end: a run marker left behind by a process
///   that is no longer there means the previous run ended abnormally.
///
/// One JSON object per line, newest last, capped at <see cref="MaxEntries"/>.
/// </summary>
public static class CrashJournal
{
    private const int MaxEntries = 200;
    private const int LogTailLines = 25;
    private const int LogTailBytes = 24 * 1024;

    private static readonly object Lock = new();
    private static string Dir => Path.GetDirectoryName(Logger.LogPath)!;
    public static string JournalPath => Path.Combine(Dir, "crashes.jsonl");
    private static string RunMarkerPath => Path.Combine(Dir, "run-state.json");

    public sealed record Entry(
        DateTime Time,
        string Source,          // "watchdog" | "startup-marker"
        string Reason,
        int Pid,
        DateTime? StartedAt,
        double UptimeSec,
        long? ExitCode,
        string? ExitCodeName,
        double? LastHeartbeatAgoSec,
        string[] LogTail);

    public static void Append(Entry entry)
    {
        lock (Lock)
        {
            try
            {
                var lines = File.Exists(JournalPath) ? File.ReadAllLines(JournalPath).ToList() : new List<string>();
                lines.Add(JsonSerializer.Serialize(entry));
                if (lines.Count > MaxEntries) lines = lines.Skip(lines.Count - MaxEntries).ToList();
                File.WriteAllLines(JournalPath, lines);
            }
            catch (Exception ex) { Logger.Log($"[CrashJournal] could not write the journal: {ex.Message}"); }
        }
    }

    /// <summary>The newest entry written within <paramref name="maxAge"/>, or null.</summary>
    public static Entry? LatestWithin(TimeSpan maxAge)
    {
        try
        {
            if (!File.Exists(JournalPath)) return null;
            var last = File.ReadLines(JournalPath).LastOrDefault(l => l.Length > 0);
            if (last == null) return null;
            var e = JsonSerializer.Deserialize<Entry>(last);
            return e != null && DateTime.Now - e.Time <= maxAge ? e : null;
        }
        catch { return null; }
    }

    // -- run marker (shell side) --------------------------------------------

    private sealed record RunMarker(int Pid, DateTime StartedAt);

    /// <summary>Called by the shell once it is the single instance. Reports a
    /// previous run that left its marker behind without being there any more,
    /// then writes its own marker.</summary>
    public static void MarkStarted()
    {
        try
        {
            if (File.Exists(RunMarkerPath))
            {
                var old = JsonSerializer.Deserialize<RunMarker>(File.ReadAllText(RunMarkerPath));
                if (old != null && old.Pid != Environment.ProcessId && !IsOurShellRunning(old.Pid) && !AlreadyJournaled(old.Pid))
                {
                    Logger.Log($"[CrashJournal] the previous run (pid {old.Pid}, started {old.StartedAt:HH:mm:ss}) ended without a clean exit");
                    Append(new Entry(DateTime.Now, "startup-marker",
                        "previous run left its marker behind: it ended without a clean exit and no watchdog recorded it",
                        old.Pid, old.StartedAt, 0, null, null, null, TailOfLog()));
                }
            }
            File.WriteAllText(RunMarkerPath,
                JsonSerializer.Serialize(new RunMarker(Environment.ProcessId, DateTime.Now)));
        }
        catch (Exception ex) { Logger.Log($"[CrashJournal] MarkStarted failed: {ex.Message}"); }
    }

    /// <summary>Called by the shell on an orderly exit.</summary>
    public static void MarkStopped()
    {
        try { File.Delete(RunMarkerPath); } catch { }
    }

    private static bool AlreadyJournaled(int pid)
    {
        try
        {
            if (!File.Exists(JournalPath)) return false;
            return File.ReadLines(JournalPath).TakeLast(20).Any(l => l.Contains($"\"Pid\":{pid},"));
        }
        catch { return false; }
    }

    private static bool IsOurShellRunning(int pid)
    {
        try
        {
            using var p = Process.GetProcessById(pid);
            return string.Equals(p.MainModule?.FileName, Environment.ProcessPath, StringComparison.OrdinalIgnoreCase);
        }
        catch { return false; }
    }

    // -- helpers --------------------------------------------------------------

    /// <summary>The last lines of caroline.log (read from the end: the file can be hundreds of MB).</summary>
    public static string[] TailOfLog()
    {
        try
        {
            using var fs = new FileStream(Logger.LogPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            long start = Math.Max(0, fs.Length - LogTailBytes);
            fs.Seek(start, SeekOrigin.Begin);
            using var sr = new StreamReader(fs);
            var lines = sr.ReadToEnd().Split('\n').Select(l => l.TrimEnd('\r')).Where(l => l.Length > 0).ToList();
            if (start > 0 && lines.Count > 0) lines.RemoveAt(0);   // cut mid-line
            return lines.TakeLast(LogTailLines).Select(l => l.Length > 400 ? l[..400] + "..." : l).ToArray();
        }
        catch { return Array.Empty<string>(); }
    }

    public static string? ExitCodeName(long code) => (uint)code switch
    {
        0 => "0 (exited normally, but nobody asked it to)",
        1 => "1 (typical of TerminateProcess / taskkill / End Task)",
        0xC0000005 => "access violation",
        0xC0000409 => "fast-fail (stack buffer overrun / abort)",
        0xC000013A => "Ctrl+C / console close",
        0xC0000142 => "DLL initialization failed (session ending?)",
        0xE0434352 => "unhandled .NET exception",
        0xE06D7363 => "unhandled C++ exception",
        0xFFFFFFFF => "-1 (killed)",
        _ => null,
    };
}

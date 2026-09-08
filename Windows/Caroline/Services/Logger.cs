using System.IO;

namespace Caroline.Services;

/// <summary>
/// Minimal file logger for the WPF shell itself -- appends timestamped
/// lines to %LocalAppData%\Caroline\caroline.log. Unlike CarolineInstaller's
/// Logger (truncated fresh each run, since installs are short-lived), this
/// one is append-only across the app's whole lifetime -- including crashes
/// and restarts -- since diagnosing "why did it crash last time" is the
/// whole point. Capped so it can't grow unbounded over weeks of use.
///
/// Separate from the backend's own Node-side console output (also piped
/// here via BackendProcess.OutputLine, see MainWindow) -- this file is the
/// one place to look for *anything* that went wrong, WPF shell or backend.
/// </summary>
public static class Logger
{
    // Raised from 2MB (2026-08-31): server.ts now logs the full content of
    // every SDK message (assistant text/tool_use, tool_result content, every
    // outgoing turn) so a session's actual history can be reconstructed
    // after the fact -- see server.ts's logSdkMessage doc comment for why
    // (Claude Code's own session transcript turned out to be lossy across
    // auto-compaction, confirmed live investigating a suspected prompt
    // injection that could no longer be traced). At 2MB that verbose a log
    // would truncate within minutes of active use, defeating the point.
    private const long MaxBytesBeforeTruncate = 300 * 1024 * 1024; // 300MB
    private static readonly object Lock = new();
    private static string? _logPath;

    public static string LogPath
    {
        get
        {
            if (_logPath is null)
            {
                var dir = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Caroline");
                Directory.CreateDirectory(dir);
                _logPath = Path.Combine(dir, "caroline.log");
            }
            return _logPath;
        }
    }

    public static void Log(string message)
    {
        var line = $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss.fff}] {message}";
        lock (Lock)
        {
            try
            {
                TruncateIfTooBigUnlocked();
                File.AppendAllText(LogPath, line + Environment.NewLine);
            }
            catch
            {
                // Logging must never be the reason the app itself fails.
            }
        }
    }

    private static void TruncateIfTooBigUnlocked()
    {
        var fi = new FileInfo(LogPath);
        if (!fi.Exists || fi.Length <= MaxBytesBeforeTruncate) return;
        // Keep the second half rather than wiping entirely -- recent history
        // (including whatever just crashed) is what actually matters.
        var bytes = File.ReadAllBytes(LogPath);
        var keepFrom = bytes.Length / 2;
        File.WriteAllBytes(LogPath, bytes[keepFrom..]);
    }
}

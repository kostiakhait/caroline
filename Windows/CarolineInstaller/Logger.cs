namespace CarolineInstaller;

/// <summary>
/// Minimal file logger -- appends timestamped lines to %TEMP%\CarolineInstaller.log,
/// flushed immediately, truncated at the start of each run. Ported from AppleKeyInstaller.
/// </summary>
internal static class Logger
{
    private static readonly object Lock = new();
    private static string? _logPath;

    /// <summary>Shown to the user in the failure dialog so a real report has somewhere to point.</summary>
    public static string LogPathForDisplay => LogPath;

    private static string LogPath
    {
        get
        {
            if (_logPath is null)
            {
                _logPath = Path.Combine(Path.GetTempPath(), "CarolineInstaller.log");
                try
                {
                    File.WriteAllText(_logPath, string.Empty);
                }
                catch
                {
                    // Best-effort -- logging must never be the reason the actual install fails.
                }
            }
            return _logPath;
        }
    }

    public static void Log(string message)
    {
        var line = $"[{DateTime.Now:HH:mm:ss.fff}] {message}";
        lock (Lock)
        {
            try
            {
                File.AppendAllText(LogPath, line + Environment.NewLine);
            }
            catch
            {
                // Logging must never be the reason the actual install fails.
            }
        }
    }
}

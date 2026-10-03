using System.IO;

namespace Caroline.NativeHost.Services;

/// <summary>
/// Minimal file logger for this process -- appends timestamped lines to the
/// SAME %LocalAppData%\Caroline\caroline.log Caroline.exe itself writes to
/// (and that its own backend's console output is piped into, see
/// Caroline.Services.Logger's own doc comment) -- deliberately a separate,
/// duplicated copy rather than a shared assembly between the two separately-
/// published processes (same established convention as e.g. CarolineInstaller's
/// own Logger, or Autostart.cs's own AppBrowserHostPort literal), but still
/// targeting the SAME physical log file: a dev diagnosing a browser-window
/// issue shouldn't have to go hunting for a second log file that only this
/// process writes to. Multiple processes appending their own lines to one
/// file is the same posture SupervisorClient.OutputLine already relies on
/// for the backend's own console output.
/// </summary>
public static class Logger
{
    private const long MaxBytesBeforeTruncate = 300 * 1024 * 1024; // 300MB, same cap as Caroline.exe's own copy
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

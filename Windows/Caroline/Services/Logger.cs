using System.Collections.Concurrent;
using System.IO;
using System.Text;

namespace Caroline.Services;

/// <summary>
/// File logger for the WPF shell itself -- appends timestamped lines to
/// %LocalAppData%\Caroline\caroline.log. Append-only across the app's whole
/// lifetime, including crashes and restarts, since diagnosing "why did it
/// crash last time" is the whole point.
///
/// Separate from the backend's own console output (also piped here via
/// SupervisorClient.OutputLine, see MainWindow) -- this file is the
/// one place to look for *anything* that went wrong, WPF shell or backend.
///
/// Redesign (2026-11-10), confirmed live from caroline.log itself: the old Log() did
/// File.AppendAllText plus a FileInfo stat PER LINE under a lock, on the single thread that reads the
/// backend's stdout, and at the size cap it ReadAllBytes'd the whole 300 MB file to cut it in half. Each
/// open/close of a 200 MB file passes through Defender, so the reader fell behind, the stdout pipe filled
/// up, and the backend's print(flush=True) blocked its event loop for 3-18 s (event_loop_blocked), after
/// which the supervisor killed it (/api/status did not respond). Lines reached this file ~10 s after their
/// own "ts". Now Log() only enqueues (never touches the disk); one background thread keeps the file open
/// and writes in batches; at the cap the file is renamed to caroline.log.1 (no copying).
/// </summary>
public static class Logger
{
    // Rotation: past this size the live file is renamed to caroline.log.1 (one previous generation, replaced
    // each time) and a fresh caroline.log starts. Verbose on purpose (see server.ts logSdkMessage's old note:
    // a session's history must be reconstructible), so two generations hold a few hundred MB of history.
    private const long MaxBytesBeforeRotate = 150L * 1024 * 1024;
    private static readonly BlockingCollection<string> Queue = new(new ConcurrentQueue<string>());
    private static readonly object StartLock = new();
    private static readonly UTF8Encoding Utf8 = new(false);
    private static Thread? _writer;
    private static string? _logPath;
    private static volatile bool _idle = true;

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
        EnsureWriter();
        try { Queue.Add(line); } catch { /* completed at shutdown: nothing to do */ }
    }

    /// <summary>Waits (bounded) until everything queued so far is on disk; call before the process ends.</summary>
    public static void Flush(int timeoutMs = 3000)
    {
        var until = Environment.TickCount64 + timeoutMs;
        while ((Queue.Count > 0 || !_idle) && Environment.TickCount64 < until) Thread.Sleep(10);
    }

    private static void EnsureWriter()
    {
        if (_writer is not null) return;
        lock (StartLock)
        {
            if (_writer is not null) return;
            _ = LogPath; // resolve + create the directory on the caller's thread
            _writer = new Thread(WriterLoop) { IsBackground = true, Name = "CarolineLogWriter" };
            _writer.Start();
            AppDomain.CurrentDomain.ProcessExit += (_, _) => Flush();
        }
    }

    private static FileStream Open()
    {
        // FileMode.Append: every write goes to the end even though the watchdog process appends to the same
        // file; FileShare.Delete: a rotation by either process (rename) never fails on the other's handle.
        return new FileStream(LogPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete, 64 * 1024);
    }

    private static void WriterLoop()
    {
        FileStream? fs = null;
        DateTime created = default;
        var sb = new StringBuilder();
        while (true)
        {
            string first;
            try { first = Queue.Take(); } catch { return; }
            _idle = false;
            sb.Clear();
            sb.Append(first).Append(Environment.NewLine);
            while (sb.Length < 1024 * 1024 && Queue.TryTake(out var next))
                sb.Append(next).Append(Environment.NewLine);
            try
            {
                // One stat per BATCH. Another process (the watchdog) may have rotated the file under our open
                // handle: then the path is a different file and we must reopen, or we'd write into caroline.log.1.
                var info = new FileInfo(LogPath);
                if (fs is not null && (!info.Exists || info.CreationTimeUtc != created))
                {
                    fs.Dispose();
                    fs = null;
                }
                if (fs is not null && fs.Length > MaxBytesBeforeRotate)
                {
                    fs.Dispose();
                    fs = null;
                    Rotate();
                }
                if (fs is null)
                {
                    if (new FileInfo(LogPath) is { Exists: true, Length: > MaxBytesBeforeRotate }) Rotate();
                    fs = Open();
                    created = new FileInfo(LogPath).CreationTimeUtc;
                }
                var bytes = Utf8.GetBytes(sb.ToString());
                fs.Write(bytes, 0, bytes.Length);
                fs.Flush();
            }
            catch
            {
                // Logging must never be the reason the app itself fails; drop this batch, retry the file next time.
                try { fs?.Dispose(); } catch { }
                fs = null;
            }
            _idle = Queue.Count == 0;
        }
    }

    private static void Rotate()
    {
        try { File.Move(LogPath, LogPath + ".1", overwrite: true); }
        catch { /* the other process rotated first, or the file is busy: next batch re-checks */ }
    }
}

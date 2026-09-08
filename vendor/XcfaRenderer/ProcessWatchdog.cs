using System.Diagnostics;
using System.Runtime.InteropServices;

namespace XcfaRenderer;

/// <summary>
/// Registry of active ffmpeg subprocesses plus an optional parent-process
/// watchdog that kills them all if a given parent PID disappears -- direct
/// port of _render_worker.py's _register_ffmpeg/_unregister_ffmpeg/
/// _kill_all_ffmpeg/_watch_parent.
///
/// In the Python original this exists because _render_worker.py runs as a
/// short-lived subprocess spawned by the Camerlengo API process: if that
/// parent dies (crash, taskkill, restart) while a render is mid-flight, the
/// worker would otherwise leave an orphaned ffmpeg encoding forever. Ported
/// as-is (not reinterpreted away as "not applicable to a library") since a
/// host application embedding XcfaRenderer can be killed the same way while
/// Encoder has a live ffmpeg child -- WatchParent lets a host opt into the
/// exact same protection by passing its own idea of "parent" (its own
/// process, a supervisor's PID, whatever it wants to tie the encode's
/// lifetime to). Encoder.Open registers/unregisters itself automatically;
/// nothing else needs to touch this type unless a host wants the watchdog.
/// </summary>
public static class ProcessWatchdog
{
    private static readonly HashSet<Process> ActiveFfmpeg = new();
    private static readonly object Gate = new();

    internal static void Register(Process proc)
    {
        lock (Gate) ActiveFfmpeg.Add(proc);
    }

    internal static void Unregister(Process proc)
    {
        lock (Gate) ActiveFfmpeg.Remove(proc);
    }

    /// <summary>Kills every currently-registered ffmpeg subprocess. Direct port of _kill_all_ffmpeg.</summary>
    public static void KillAllFfmpeg()
    {
        Process[] procs;
        lock (Gate) procs = ActiveFfmpeg.ToArray();
        foreach (var p in procs)
        {
            try { p.Kill(entireProcessTree: true); } catch { /* already gone */ }
        }
    }

    /// <summary>
    /// Starts a background daemon thread that calls KillAllFfmpeg() and then
    /// onParentGone() if parentPid stops being a live process. Direct port
    /// of _watch_parent (including its Windows-specific OpenProcess liveness
    /// check -- the Python original's POSIX os.kill(pid, 0) branch is not
    /// ported since this project targets Windows only, same as the rest of
    /// Caroline).
    /// </summary>
    public static void WatchParent(int parentPid, TimeSpan? interval = null, Action? onParentGone = null)
    {
        var period = interval ?? TimeSpan.FromSeconds(3);
        var thread = new Thread(() =>
        {
            while (true)
            {
                Thread.Sleep(period);
                if (!IsProcessAlive(parentPid))
                {
                    KillAllFfmpeg();
                    onParentGone?.Invoke();
                    return;
                }
            }
        })
        {
            IsBackground = true,
            Name = "xcfa-parent-watcher",
        };
        thread.Start();
    }

    private const uint Synchronize = 0x00100000;

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint dwDesiredAccess, bool bInheritHandle, int dwProcessId);

    [DllImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseHandle(IntPtr hObject);

    private static bool IsProcessAlive(int pid)
    {
        var handle = OpenProcess(Synchronize, false, pid);
        if (handle == IntPtr.Zero) return false;
        CloseHandle(handle);
        return true;
    }
}

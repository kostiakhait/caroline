using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;

namespace Caroline.Native;

/// <summary>
/// Two guards against the supervisor/backend surviving the app that spawned
/// them (incident 2026-10-28: Caroline.exe vanished without a trace, its
/// pythonw supervisor and backend lived on holding ports 48766/48765, and
/// every relaunch of the supervisor died on Errno 10048 -- 1493 restarts in
/// a row, the app never came up again until the orphans were killed by hand).
///
/// 1. <see cref="Attach"/>: an OS job object with KILL_ON_JOB_CLOSE. The
///    supervisor (and so the backend and every CLI it spawns) is put into it,
///    so when this process dies in ANY way -- crash, End Task -- Windows
///    kills the whole tree.
/// 2. <see cref="ReapStaleListeners"/>: belt and braces for orphans that
///    already exist (a previous build without the job object, or a job that
///    could not be created). Kills whatever bundled-runtime python process is
///    listening on our two ports before the supervisor is spawned.
/// </summary>
internal static class SupervisorProcessGuard
{
    private static IntPtr _job;

    /// <summary>Puts the process into the kill-on-close job. Never throws;
    /// returns false (and says why) if the job could not be used.</summary>
    public static bool Attach(Process process, Action<string> log)
    {
        try
        {
            if (_job == IntPtr.Zero)
            {
                _job = CreateJobObject(IntPtr.Zero, null);
                if (_job == IntPtr.Zero) throw new Win32Exception();
                var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                int size = Marshal.SizeOf<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>();
                IntPtr buf = Marshal.AllocHGlobal(size);
                try
                {
                    Marshal.StructureToPtr(info, buf, false);
                    if (!SetInformationJobObject(_job, JobObjectExtendedLimitInformation, buf, (uint)size))
                        throw new Win32Exception();
                }
                finally { Marshal.FreeHGlobal(buf); }
            }
            if (!AssignProcessToJobObject(_job, process.Handle)) throw new Win32Exception();
            log($"[SupervisorGuard] supervisor pid={process.Id} is in a kill-on-close job");
            return true;
        }
        catch (Exception ex)
        {
            log($"[SupervisorGuard] could not put the supervisor into a kill-on-close job: {ex.Message}");
            return false;
        }
    }

    /// <summary>Kills bundled-runtime python processes that listen on the
    /// given ports. Only processes whose executable sits in our own
    /// runtime\python folder are touched -- never an unrelated program that
    /// happens to use the port.</summary>
    public static void ReapStaleListeners(string runtimePythonDir, int[] ports, Action<string> log)
    {
        try
        {
            var psi = new ProcessStartInfo("netstat.exe", "-ano -p TCP")
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
            };
            using var netstat = Process.Start(psi);
            if (netstat == null) return;
            string output = netstat.StandardOutput.ReadToEnd();
            netstat.WaitForExit(5000);

            var pids = new HashSet<int>();
            foreach (var raw in output.Split('\n'))
            {
                var cols = raw.Split(' ', StringSplitOptions.RemoveEmptyEntries);
                // TCP  127.0.0.1:48766  0.0.0.0:0  LISTENING  32124
                if (cols.Length < 5 || cols[0] != "TCP" || cols[3] != "LISTENING") continue;
                int colon = cols[1].LastIndexOf(':');
                if (colon < 0 || !int.TryParse(cols[1].AsSpan(colon + 1), out int port) || Array.IndexOf(ports, port) < 0) continue;
                if (int.TryParse(cols[4].Trim(), out int pid) && pid != Environment.ProcessId) pids.Add(pid);
            }

            string runtimeDir = Path.GetFullPath(runtimePythonDir).TrimEnd('\\') + "\\";
            foreach (int pid in pids)
            {
                try
                {
                    using var p = Process.GetProcessById(pid);
                    string? exe = p.MainModule?.FileName;
                    if (exe == null || !exe.StartsWith(runtimeDir, StringComparison.OrdinalIgnoreCase))
                    {
                        log($"[SupervisorGuard] pid={pid} holds a Caroline port but is not our runtime ({exe ?? "unknown"}) -- left alone");
                        continue;
                    }
                    log($"[SupervisorGuard] killing stale {p.ProcessName} pid={pid} (started {p.StartTime:HH:mm:ss}) that holds a Caroline port");
                    p.Kill(entireProcessTree: true);
                    p.WaitForExit(3000);
                }
                catch (Exception ex) { log($"[SupervisorGuard] could not reap pid={pid}: {ex.Message}"); }
            }
        }
        catch (Exception ex) { log($"[SupervisorGuard] port scan failed: {ex.Message}"); }
    }

    private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;
    private const int JobObjectExtendedLimitInformation = 9;

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IO_COUNTERS
    {
        public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
        public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr attrs, string? name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint size);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
}

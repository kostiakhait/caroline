using System.Runtime.InteropServices;

namespace Caroline.Native;

/// <summary>
/// Pure WinAPI process enumeration (kernel32.dll's Toolhelp32Snapshot) -- per
/// explicit instruction (2026-09-06): no system call like "list every
/// process's parent" or "kill this pid" may ever be done by spawning an
/// external process (PowerShell, taskkill, or anything else). This is the
/// direct native equivalent, same DllImport/struct-marshaling style as
/// RestartManagerHelper.cs's own rstrtmgr.dll wrapper in CarolineInstaller.
/// Exposed to the Node backend over AppBrowserHost's existing local HTTP
/// bridge (see its own /process_list and /kill_process handlers) -- that's
/// an already-running sibling process answering over a socket, not a new
/// process being spawned for the call.
/// </summary>
public static class ProcessTreeHelper
{
    private const uint TH32CS_SNAPPROCESS = 0x00000002;
    private static readonly IntPtr InvalidHandleValue = new(-1);

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESSENTRY32
    {
        public uint dwSize;
        public uint cntUsage;
        public uint th32ProcessID;
        public IntPtr th32DefaultHeapID;
        public uint th32ModuleID;
        public uint cntThreads;
        public uint th32ParentProcessID;
        public int pcPriClassBase;
        public uint dwFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        public string szExeFile;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr CreateToolhelp32Snapshot(uint dwFlags, uint th32ProcessID);

    [DllImport("kernel32.dll")]
    private static extern bool Process32First(IntPtr hSnapshot, ref PROCESSENTRY32 lppe);

    [DllImport("kernel32.dll")]
    private static extern bool Process32Next(IntPtr hSnapshot, ref PROCESSENTRY32 lppe);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr hObject);

    public sealed record ProcessEntry(int Pid, int ParentPid, string Name);

    /// <summary>Every currently-running process on the machine (pid, parent pid, exe name),
    /// via one kernel32.dll snapshot. Returns an empty list on any API failure -- never
    /// throws; this is meant to be at least as safe as the PowerShell/WMI calls it replaces.</summary>
    public static List<ProcessEntry> ListAll()
    {
        var result = new List<ProcessEntry>();
        var snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if (snapshot == IntPtr.Zero || snapshot == InvalidHandleValue) return result;
        try
        {
            var entry = new PROCESSENTRY32 { dwSize = (uint)Marshal.SizeOf<PROCESSENTRY32>() };
            if (!Process32First(snapshot, ref entry)) return result;
            do
            {
                result.Add(new ProcessEntry((int)entry.th32ProcessID, (int)entry.th32ParentProcessID, entry.szExeFile));
            } while (Process32Next(snapshot, ref entry));
        }
        catch
        {
            // Best-effort, same posture as RestartManagerHelper.WhoIsLocking -- a failure
            // here must never be the reason a caller's own retry/cleanup logic breaks.
        }
        finally
        {
            CloseHandle(snapshot);
        }
        return result;
    }
}

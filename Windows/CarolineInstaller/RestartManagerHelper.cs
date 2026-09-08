using System.Linq;
using System.Runtime.InteropServices;

namespace CarolineInstaller;

/// <summary>
/// Diagnostic-only wrapper around Windows' Restart Manager API (rstrtmgr.dll) -- the exact
/// same mechanism a real installer's own "this file is in use by: X" dialog is built on.
/// Added (2026-09-04) after several rounds of guessing at what was locking AppDir during
/// extraction (Caroline.exe itself, its node.exe/claude.exe descendants, stray WebView2
/// processes) still didn't stop a live "used by another process" failure from recurring --
/// this replaces guessing with an actual answer: given a set of files, it returns exactly
/// which running process(es) currently hold any of them open, by name and pid. Purely
/// diagnostic (logged, not acted on automatically) -- best-effort throughout, any failure
/// in the API itself must never block the real retry logic already in Program.cs.
/// </summary>
internal static class RestartManagerHelper
{
    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    private static extern int RmStartSession(out uint pSessionHandle, int dwSessionFlags, string strSessionKey);

    [DllImport("rstrtmgr.dll")]
    private static extern int RmEndSession(uint pSessionHandle);

    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    private static extern int RmRegisterResources(uint pSessionHandle, uint nFiles, string[] rgsFilenames,
        uint nApplications, RM_UNIQUE_PROCESS[]? rgApplications, uint nServices, string[]? rgsServiceNames);

    [DllImport("rstrtmgr.dll")]
    private static extern int RmGetList(uint dwSessionHandle, out uint pnProcInfoNeeded, ref uint pnProcInfo,
        [In, Out] RM_PROCESS_INFO[]? rgAffectedApps, ref uint lpdwRebootReasons);

    private const int ERROR_MORE_DATA = 234;
    private const int CCH_RM_MAX_APP_NAME = 255;
    private const int CCH_RM_MAX_SVC_NAME = 63;

    [StructLayout(LayoutKind.Sequential)]
    private struct RM_UNIQUE_PROCESS
    {
        public int dwProcessId;
        public System.Runtime.InteropServices.ComTypes.FILETIME ProcessStartTime;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct RM_PROCESS_INFO
    {
        public RM_UNIQUE_PROCESS Process;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = CCH_RM_MAX_APP_NAME + 1)]
        public string strAppName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = CCH_RM_MAX_SVC_NAME + 1)]
        public string strServiceShortName;
        public int ApplicationType;
        public uint AppStatus;
        public uint TSSessionId;
        [MarshalAs(UnmanagedType.Bool)]
        public bool bRestartable;
    }

    /// <summary>Returns "ProcessName (pid=N)" for every currently-running process holding ANY
    /// of the given files open. Files that don't exist or aren't actually locked are silently
    /// skipped by the API itself -- no need to pre-filter. Caps the file list at 2000 entries
    /// (a node_modules tree can have tens of thousands; RM itself has no documented hard limit,
    /// but this is meant to be a quick diagnostic snapshot, not an exhaustive one -- a lock
    /// holder is very likely to show up well within the first couple thousand files checked).
    /// Returns an empty list on any API failure -- never throws.</summary>
    public static List<string> WhoIsLocking(IReadOnlyList<string> filePaths)
    {
        var result = new List<string>();
        if (filePaths.Count == 0) return result;
        var files = filePaths.Count > 2000 ? filePaths.Take(2000).ToArray() : filePaths.ToArray();

        var key = Guid.NewGuid().ToString();
        if (RmStartSession(out var handle, 0, key) != 0) return result;
        try
        {
            if (RmRegisterResources(handle, (uint)files.Length, files, 0, null, 0, null) != 0) return result;

            uint pnProcInfoNeeded = 0, pnProcInfo = 0, reasons = 0;
            var res = RmGetList(handle, out pnProcInfoNeeded, ref pnProcInfo, null, ref reasons);
            if (res != 0 && res != ERROR_MORE_DATA) return result;
            if (pnProcInfoNeeded == 0) return result;

            pnProcInfo = pnProcInfoNeeded;
            var processInfo = new RM_PROCESS_INFO[pnProcInfo];
            res = RmGetList(handle, out pnProcInfoNeeded, ref pnProcInfo, processInfo, ref reasons);
            if (res != 0) return result;

            for (var i = 0; i < pnProcInfo; i++)
            {
                result.Add($"{processInfo[i].strAppName} (pid={processInfo[i].Process.dwProcessId})");
            }
        }
        catch (Exception ex)
        {
            // Best-effort -- this is a diagnostic aid, never allowed to be the reason the
            // actual install/update fails on top of whatever it was already failing on. Still
            // logged: silently returning the empty `result` here is indistinguishable from a
            // genuine "nothing has it locked" finding at the call site, which would be actively
            // misleading during exactly the kind of lock investigation this exists for.
            Logger.Log($"RestartManagerHelper.WhoIsLocking: Restart Manager query itself failed (reporting no lockers found, but that may just mean this diagnostic broke, not that nothing is locked): {ex.Message}");
        }
        finally
        {
            RmEndSession(handle);
        }
        return result;
    }
}

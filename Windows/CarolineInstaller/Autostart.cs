using System.Diagnostics;
using System.Linq;
using System.Management;
using System.Net.Http;
using Microsoft.Win32;

namespace CarolineInstaller;

/// <summary>
/// Registers the freshly-installed exe for autostart and first tears down
/// every earlier copy of the app -- match by exe base filename, stop the
/// process if it's running, delete old files. Ported from AppleKeyInstaller.
/// Safe to call when nothing was ever installed before (idempotent).
/// </summary>
internal static class Autostart
{
    private const string RunKeyPath = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string AppName = "Caroline";
    private const string ClientExeName = "Caroline.exe";
    private const string StartupShortcutName = "Caroline.lnk";

    public static void RemovePreInstallerCopies()
    {
        RemoveStartupFolderShortcut();

        using var key = Registry.CurrentUser.OpenSubKey(RunKeyPath, writable: true);
        if (key is null)
        {
            return;
        }

        var newInstallExe = Path.GetFullPath(AppPaths.ClientExe);
        var staleExePaths = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        foreach (var valueName in key.GetValueNames())
        {
            var raw = key.GetValue(valueName) as string;
            if (string.IsNullOrWhiteSpace(raw))
            {
                continue;
            }
            var exePath = ExtractExePath(raw);
            if (!string.Equals(Path.GetFileName(exePath), ClientExeName, StringComparison.OrdinalIgnoreCase))
            {
                continue; // unrelated Run entry, leave it alone
            }
            Logger.Log($"Autostart: found stale Run entry '{valueName}' -> {exePath}");
            key.DeleteValue(valueName, throwOnMissingValue: false);

            var fullPath = TryGetFullPath(exePath);
            if (fullPath is not null && !string.Equals(fullPath, newInstallExe, StringComparison.OrdinalIgnoreCase))
            {
                staleExePaths.Add(fullPath);
            }
        }

        foreach (var oldExe in staleExePaths)
        {
            KillProcessesRunningFrom(oldExe);
            TryDeleteOldInstall(oldExe);
        }
    }

    /// <summary>Stops any OTHER running CarolineInstaller.exe (self-excluded by PID).</summary>
    public static void StopOtherInstallerInstances()
    {
        var selfId = Environment.ProcessId;
        foreach (var proc in Process.GetProcessesByName("CarolineInstaller"))
        {
            if (proc.Id == selfId)
            {
                continue;
            }
            try
            {
                Logger.Log($"StopOtherInstallerInstances: stopping pid={proc.Id}");
                proc.Kill(entireProcessTree: true);
                proc.WaitForExit(5000);
            }
            catch (Exception ex)
            {
                Logger.Log($"StopOtherInstallerInstances: couldn't stop pid={proc.Id}: {ex.Message}");
            }
        }
    }

    // Caroline\Native\AppBrowserHost.cs's own Port const -- kept as a separate literal here
    // (not shared code between the two projects) since this is the one place the installer
    // needs it.
    private const int AppBrowserHostPort = 8767;

    /// <summary>How many times to ask (and verify) before giving up on the polite path and
    /// escalating to Kill() -- per explicit instruction (2026-09-06): confirmed live, twice in
    /// one day, that a single 10s grace window wasn't enough and the installer fell straight
    /// through to extraction with the previous process's exit unconfirmed, then hit "used by
    /// another process" on AppDir for the entire ~110s retry budget. Each attempt gets its own
    /// fresh /shutdown request (not just a longer wait on the first one) in case the endpoint
    /// call itself is what's flaky, not just the exit.</summary>
    private const int MaxGracefulShutdownAttempts = 5;

    /// <summary>One attempt: ask Caroline to exit via her own /shutdown endpoint (see
    /// AppBrowserHost.cs's Dispatch), then watch for up to 10s for "Caroline" to actually stop
    /// appearing in the process list. Returns true the moment it's confirmed gone (including
    /// if it already wasn't running when called) -- false if the request failed outright or
    /// the process is still there after the wait, in which case the caller decides whether to
    /// retry or escalate.</summary>
    private static async Task<bool> TryGracefulShutdownOnceAsync()
    {
        if (!Process.GetProcessesByName("Caroline").Any())
        {
            return true; // nothing running -- nothing to ask
        }
        try
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(3) };
            var resp = await http.PostAsync($"http://127.0.0.1:{AppBrowserHostPort}/shutdown", new StringContent(""));
            if (!resp.IsSuccessStatusCode)
            {
                Logger.Log($"TryGracefulShutdownOnceAsync: /shutdown returned {(int)resp.StatusCode}");
                return false;
            }
        }
        catch (Exception ex)
        {
            Logger.Log($"TryGracefulShutdownOnceAsync: request failed (older Caroline build without /shutdown, or not actually running): {ex.Message}");
            return false;
        }

        Logger.Log("TryGracefulShutdownOnceAsync: /shutdown accepted, waiting for the process to actually exit...");
        var deadline = DateTime.UtcNow.AddSeconds(10);
        while (DateTime.UtcNow < deadline)
        {
            if (!Process.GetProcessesByName("Caroline").Any())
            {
                Logger.Log("TryGracefulShutdownOnceAsync: process exited gracefully");
                return true;
            }
            await Task.Delay(250);
        }
        Logger.Log("TryGracefulShutdownOnceAsync: process did not exit within 10s");
        return false;
    }

    /// <summary>Stops any currently running Caroline.exe, regardless of path -- AND its
    /// backend's own node.exe/claude.exe descendants specifically, by path rather than by
    /// name (node.exe/claude.exe are much too generic a name to kill system-wide). Confirmed
    /// live (2026-09-03) as a real gap: Kill(entireProcessTree: true) on Caroline.exe alone
    /// still left the subsequent Directory.Delete(AppDir) failing with "used by another
    /// process" partway through a later extraction -- entireProcessTree kill walks the OS
    /// parent-child chain at the moment it runs, which can miss a grandchild that was mid-
    /// spawn or got reparented; killing everything under our own install path directly,
    /// independent of that chain, closes the gap regardless of the reason.
    ///
    /// Per explicit instruction (2026-09-06): now genuinely MAKES SURE the previous process is
    /// gone before ever reaching for Kill() -- up to MaxGracefulShutdownAttempts separate
    /// /shutdown-and-verify cycles (each its own fresh request, ~10s of its own to actually
    /// exit), not just one attempt with a single wait. Only after all of those still find
    /// Caroline running does this fall through to the unconditional hard-kill pass below.</summary>
    public static async Task StopRunningClientAsync()
    {
        for (var attempt = 1; attempt <= MaxGracefulShutdownAttempts; attempt++)
        {
            Logger.Log($"StopRunningClient: graceful shutdown attempt {attempt}/{MaxGracefulShutdownAttempts}");
            if (await TryGracefulShutdownOnceAsync())
            {
                Logger.Log("StopRunningClient: confirmed no running Caroline process -- skipping the hard-kill pass");
                break;
            }
            if (attempt == MaxGracefulShutdownAttempts)
            {
                Logger.Log($"StopRunningClient: still running after {MaxGracefulShutdownAttempts} graceful attempts -- escalating to Kill()");
            }
        }

        var carolineProcs = Process.GetProcessesByName("Caroline");
        Logger.Log($"StopRunningClient: found {carolineProcs.Length} 'Caroline' process(es)");
        foreach (var proc in carolineProcs)
        {
            try
            {
                var procPath = proc.MainModule?.FileName;
                Logger.Log($"StopRunningClient: stopping running instance pid={proc.Id} ({procPath ?? "unknown path"})");
                proc.Kill(entireProcessTree: true);
                proc.WaitForExit(8000);
            }
            catch (Exception ex)
            {
                Logger.Log($"StopRunningClient: couldn't stop pid={proc.Id}: {ex.Message}");
            }
        }

        // Every process this loop even LOOKS AT gets logged now -- per explicit
        // instruction (2026-09-06), the previous silent `continue`s (path
        // unreadable, or just not under AppPaths.Root) meant a real accumulation
        // of stuck/lingering processes over many hours left literally zero trace
        // here: nothing to tell us whether this loop ever ran, found candidates,
        // or correctly/incorrectly skipped them.
        foreach (var name in new[] { "node", "claude" })
        {
            var procs = Process.GetProcessesByName(name);
            Logger.Log($"StopRunningClient: found {procs.Length} '{name}' process(es) on the machine");
            foreach (var proc in procs)
            {
                string? procPath;
                try
                {
                    procPath = proc.MainModule?.FileName;
                }
                catch (Exception ex)
                {
                    Logger.Log($"StopRunningClient: {name} pid={proc.Id} -- couldn't read its path ({ex.Message}), leaving it alone");
                    continue; // can't inspect it (exited already, access denied, ...) -- not ours to touch
                }
                if (procPath is null || !procPath.StartsWith(AppPaths.Root, StringComparison.OrdinalIgnoreCase))
                {
                    Logger.Log($"StopRunningClient: {name} pid={proc.Id} path='{procPath ?? "(null)"}' -- not under {AppPaths.Root}, leaving it alone");
                    continue; // some unrelated node/claude process on this machine -- leave it alone
                }
                try
                {
                    Logger.Log($"StopRunningClient: stopping lingering backend process pid={proc.Id} ({procPath})");
                    proc.Kill(entireProcessTree: true);
                    proc.WaitForExit(8000);
                }
                catch (Exception ex)
                {
                    Logger.Log($"StopRunningClient: couldn't stop pid={proc.Id}: {ex.Message}");
                }
            }
        }

        KillStrayWebView2Processes();

        // Even a confirmed-exited process can leave the OS finishing an asynchronous
        // unmap of its files' memory-mapped image sections for a brief moment afterward
        // (the same real-world quirk ShortNerdCat's own updater.go was written to work
        // around, see its applyClientSelfReplace/FinishClientSelfReplace doc comments) --
        // a short grace pause here, plus the retry-with-backoff around the extraction step
        // itself (see Program.cs), covers it without guessing at an exact duration.
        Thread.Sleep(500);
    }

    /// <summary>Kills any msedgewebview2.exe process belonging to one of Caroline's OWN
    /// WebView2 profiles (chat tabs, AppBrowserWindow instances, Visual Mode, document/
    /// office/payment viewers -- every one of them uses a userDataFolder somewhere under
    /// "%APPDATA%\Caroline\webview2*", confirmed by reading every CoreWebView2Environment.
    /// CreateAsync call site directly). Confirmed live (2026-09-03) as a real gap even after
    /// the node.exe/claude.exe cleanup above: msedgewebview2.exe itself runs from Microsoft's
    /// own Edge WebView2 Runtime install path, nowhere under Caroline's own install root, so
    /// neither the by-name "Caroline" kill nor the by-path node/claude kill above can ever
    /// catch it -- yet its renderer processes can still be holding a file open under AppDir
    /// (loaded via file:// navigation to wwwroot/*.html) well after Caroline.exe itself has
    /// exited, if they were orphaned rather than cleanly torn down as part of it. Process.
    /// MainModule only exposes a process's own exe path, never its arguments, so telling
    /// "one of ours" apart from any other WebView2-hosting app on the machine needs the
    /// actual command line -- via WMI (Win32_Process.CommandLine), the standard .NET way to
    /// read an already-running external process's arguments.</summary>
    private static void KillStrayWebView2Processes()
    {
        try
        {
            using var searcher = new ManagementObjectSearcher(
                "SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name = 'msedgewebview2.exe'");
            using var results = searcher.Get();
            var all = results.Cast<ManagementObject>().ToList();
            Logger.Log($"KillStrayWebView2Processes: found {all.Count} msedgewebview2.exe process(es) on the machine");
            foreach (var mo in all)
            {
                var commandLine = mo["CommandLine"] as string;
                if (commandLine is null || commandLine.IndexOf(@"\Caroline\webview2", StringComparison.OrdinalIgnoreCase) < 0)
                {
                    continue; // not one of Caroline's own profiles -- leave it alone
                }
                var pid = (uint)mo["ProcessId"];
                try
                {
                    using var proc = Process.GetProcessById((int)pid);
                    Logger.Log($"StopRunningClient: stopping stray WebView2 process pid={pid}");
                    proc.Kill(entireProcessTree: true);
                    proc.WaitForExit(5000);
                }
                catch (Exception ex)
                {
                    Logger.Log($"StopRunningClient: couldn't stop stray WebView2 pid={pid}: {ex.Message}");
                }
            }
        }
        catch (Exception ex)
        {
            // WMI itself failing (service not running, etc.) must never be the reason the
            // rest of the install/update fails -- best-effort, same as everything else here.
            Logger.Log($"KillStrayWebView2Processes: WMI query failed (ignored): {ex.Message}");
        }
    }

    public static void Register()
    {
        using var key = Registry.CurrentUser.CreateSubKey(RunKeyPath);
        key.SetValue(AppName, $"\"{AppPaths.ClientExe}\"");
        Logger.Log($"Autostart: registered {AppPaths.ClientExe}");
    }

    private static void RemoveStartupFolderShortcut()
    {
        try
        {
            var lnk = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.Startup), StartupShortcutName);
            if (File.Exists(lnk))
            {
                File.Delete(lnk);
                Logger.Log($"Autostart: removed old Startup-folder shortcut {lnk}");
            }
        }
        catch (Exception ex)
        {
            Logger.Log($"Autostart: couldn't remove Startup-folder shortcut: {ex.Message}");
        }
    }

    private static void KillProcessesRunningFrom(string exePath)
    {
        foreach (var proc in Process.GetProcessesByName("Caroline"))
        {
            try
            {
                var procPath = proc.MainModule?.FileName;
                if (procPath is not null && string.Equals(procPath, exePath, StringComparison.OrdinalIgnoreCase))
                {
                    Logger.Log($"Autostart: stopping running old copy pid={proc.Id} ({procPath})");
                    proc.Kill(entireProcessTree: true);
                    proc.WaitForExit(5000);
                }
            }
            catch (Exception ex)
            {
                Logger.Log($"Autostart: couldn't inspect/kill pid={proc.Id}: {ex.Message}");
            }
        }
    }

    private static void TryDeleteOldInstall(string exePath)
    {
        try
        {
            var dir = Path.GetDirectoryName(exePath);
            File.Delete(exePath);
            Logger.Log($"Autostart: deleted old exe {exePath}");

            // Only remove the containing directory if this install owns it exclusively
            // (an "app" folder under our own Root) -- never risk deleting something else.
            if (dir is not null && Directory.Exists(dir) &&
                dir.StartsWith(AppPaths.Root, StringComparison.OrdinalIgnoreCase))
            {
                Directory.Delete(dir, recursive: true);
                Logger.Log($"Autostart: removed old install directory {dir}");
            }
        }
        catch (Exception ex)
        {
            Logger.Log($"Autostart: couldn't delete old install at {exePath}: {ex.Message}");
        }
    }

    private static string ExtractExePath(string value)
    {
        value = value.Trim();
        if (value.StartsWith('"'))
        {
            var end = value.IndexOf('"', 1);
            if (end > 0)
            {
                return value[1..end];
            }
        }
        var spaceIdx = value.IndexOf(' ');
        return spaceIdx >= 0 ? value[..spaceIdx] : value;
    }

    private static string? TryGetFullPath(string path)
    {
        try
        {
            return Path.GetFullPath(path);
        }
        catch (Exception ex)
        {
            Logger.Log($"Autostart: TryGetFullPath('{path}') failed, this stale Run entry's exe won't be killed/cleaned up: {ex.Message}");
            return null;
        }
    }
}

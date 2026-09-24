using System.ComponentModel;
using System.Diagnostics;

namespace CarolineInstaller;

/// <summary>
/// Adds Windows Defender path exclusions for Caroline's own folders, via ONE
/// UAC prompt scoped to just this step (the installer itself stays a normal,
/// per-user, non-elevated process -- see Program.cs's header comment).
///
/// Why (2026-09-23, measured live on a real machine): with Defender's
/// real-time/cloud protection scanning every FIRST open of a file, Python's
/// `import app.chat_session` -- ~1500 small files -- took 86 s to 7 minutes at
/// ~0.1 s of CPU (first open of any file cost 15-60 ms, repeat opens 0.1 ms).
/// That made every backend (re)start take minutes, and stalled the backend's
/// own event loop whenever it touched not-yet-scanned files, which the
/// health-watchdog then read as "frozen" and restarted -- users saw "Trouble
/// reconnecting" for minutes.
///
/// Scope, per explicit instruction: Caroline's own folder plus the Claude CLI's
/// transcript directory (%USERPROFILE%\.claude\projects, which the backend reads
/// on every session start). The Codex home (~\.codex) is deliberately NOT
/// excluded: it holds auth.json and third-party plugin code Codex downloads
/// itself, exactly what an AV should keep scanning, and it is small.
///
/// Never throws and never fails the install -- an exclusion is an optimisation.
/// Not attempted in --silent-update mode (a background self-update must never
/// pop a UAC prompt); the outcome is remembered in state.json so a decision is
/// never asked twice.
/// </summary>
internal static class DefenderExclusion
{
    private const string Added = "added";
    private const string Declined = "declined";
    private const string Unavailable = "unavailable";
    private const string Failed = "failed";
    private const int ErrorCancelled = 1223; // the user said "No" at the UAC prompt

    public static string[] ExcludedPaths() => new[]
    {
        AppPaths.Root,
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".claude", "projects"),
    };

    public static async Task EnsureAsync(bool silent, CancellationToken ct)
    {
        try
        {
            var state = InstallStateStore.Load();
            // "added"/"declined" are final -- never re-ask. "unavailable"/"failed" retry on the
            // next interactive run (cheap: no UAC prompt unless Defender is actually usable).
            if (state.DefenderExclusion is Added or Declined)
            {
                Logger.Log($"DefenderExclusion: already decided ({state.DefenderExclusion}), skipping.");
                return;
            }
            if (silent)
            {
                Logger.Log("DefenderExclusion: --silent-update run, not prompting for elevation.");
                return;
            }

            var outcome = await Task.Run(TryAdd, ct);
            Logger.Log($"DefenderExclusion: outcome={outcome}");
            state = InstallStateStore.Load();
            state.DefenderExclusion = outcome;
            InstallStateStore.Save(state);
        }
        catch (Exception ex)
        {
            Logger.Log($"DefenderExclusion: unexpected failure (ignored, install continues): {ex}");
        }
    }

    private static string TryAdd()
    {
        // Only worth a UAC prompt if Defender is actually the active, running AV --
        // a machine with a third-party AV (Defender passive/off) gets no prompt.
        if (!DefenderIsActive()) return Unavailable;

        var list = string.Join(",", ExcludedPaths().Select(p => "'" + p.Replace("'", "''") + "'"));
        // -ErrorAction Stop so a failure becomes a non-zero exit code we can see.
        var script = $"try {{ Add-MpPreference -ExclusionPath {list} -ErrorAction Stop }} catch {{ exit 1 }}";
        var psi = new ProcessStartInfo("powershell.exe", $"-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command \"{script}\"")
        {
            UseShellExecute = true, // required for Verb=runas
            Verb = "runas",
            WindowStyle = ProcessWindowStyle.Hidden,
        };
        try
        {
            using var p = Process.Start(psi);
            if (p is null) return Failed;
            p.WaitForExit();
            return p.ExitCode == 0 ? Added : Failed;
        }
        catch (Win32Exception ex) when (ex.NativeErrorCode == ErrorCancelled)
        {
            return Declined;
        }
    }

    private static bool DefenderIsActive()
    {
        try
        {
            var psi = new ProcessStartInfo("powershell.exe",
                "-NoProfile -ExecutionPolicy Bypass -Command \"$s=Get-MpComputerStatus -ErrorAction Stop; if($s.AMServiceEnabled -and $s.RealTimeProtectionEnabled){exit 0}else{exit 1}\"")
            {
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            using var p = Process.Start(psi);
            if (p is null) return false;
            if (!p.WaitForExit(30_000)) { try { p.Kill(); } catch { } return false; }
            return p.ExitCode == 0;
        }
        catch (Exception ex)
        {
            Logger.Log($"DefenderExclusion: Defender status check failed ({ex.Message}) -- treating as unavailable.");
            return false;
        }
    }
}

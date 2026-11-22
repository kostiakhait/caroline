using System.Diagnostics;

namespace CarolineInstaller.Dependencies;

/// <summary>
/// Runs `npm install` inside the messenger-sidecar folder the Makefile
/// publishes as a sibling of backend-py (see Makefile's own "messenger-
/// sidecar" recipe and supervisor.py's CAROLINE_MESSENGER_SIDECAR_DIR).
/// One direct dependency (package.json) -- installing at setup time, same
/// as WhatsappSidecarInstaller does for its own sidecar, rather than
/// committing node_modules to git or bundling it at build time.
///
/// Requires NodeInstaller to have already run (uses AppPaths.NodeDir's own
/// npm-cli.js via node.exe, never relies on "npm" being resolvable on PATH,
/// same isolation principle as every other runtime here).
/// </summary>
internal static class MessengerSidecarInstaller
{
    /// <param name="appDir">The app folder to set up; null: the active one. An update sets up the new folder before Caroline stops
    /// (2026-10-31), while the running one is still in use.</param>
    public static async Task InstallAsync(CancellationToken ct, string? appDir = null)
    {
        var sidecarDir = Path.Combine(appDir ?? AppPaths.AppDir, "messenger-sidecar");
        var nodeModulesMarker = Path.Combine(sidecarDir, "node_modules", ".caroline-installed");
        if (!Directory.Exists(sidecarDir))
        {
            Logger.Log($"MessengerSidecarInstaller: {sidecarDir} not found (older build without the sidecar?) -- skipping, Messenger just won't be available.");
            return;
        }
        if (File.Exists(nodeModulesMarker))
        {
            Logger.Log("MessengerSidecarInstaller: already installed, skipping");
            return;
        }

        var npmCliJs = Path.Combine(AppPaths.NodeDir, "node_modules", "npm", "bin", "npm-cli.js");
        if (!File.Exists(npmCliJs))
        {
            throw new InvalidOperationException($"npm-cli.js not found at {npmCliJs} -- Node.js install looks incomplete.");
        }

        var psi = new ProcessStartInfo
        {
            FileName = AppPaths.NodeExe,
            WorkingDirectory = sidecarDir,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        psi.ArgumentList.Add(npmCliJs);
        psi.ArgumentList.Add("install");
        psi.ArgumentList.Add("--no-audit");
        psi.ArgumentList.Add("--no-fund");
        psi.ArgumentList.Add("--omit=dev");

        Logger.Log($"MessengerSidecarInstaller: running npm install in {sidecarDir}");
        using var proc = Process.Start(psi) ?? throw new InvalidOperationException("failed to start node.exe for npm install");
        var stdout = await proc.StandardOutput.ReadToEndAsync(ct);
        var stderr = await proc.StandardError.ReadToEndAsync(ct);
        await proc.WaitForExitAsync(ct);
        Logger.Log($"MessengerSidecarInstaller: npm install exit={proc.ExitCode}\n{stdout}\n{stderr}");
        if (proc.ExitCode != 0)
        {
            throw new InvalidOperationException($"npm install failed (exit {proc.ExitCode}): {stderr}\n{stdout}");
        }

        Directory.CreateDirectory(Path.GetDirectoryName(nodeModulesMarker)!);
        await File.WriteAllTextAsync(nodeModulesMarker, DateTime.UtcNow.ToString("O"), ct);
        Logger.Log("MessengerSidecarInstaller: npm install complete");
    }
}

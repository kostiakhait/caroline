using System.Diagnostics;

namespace CarolineInstaller.Dependencies;

/// <summary>
/// Runs `npm install` inside the whatsapp-sidecar folder the Makefile
/// publishes as a sibling of backend-py (see Makefile's own "whatsapp-
/// sidecar" recipe and supervisor.py's CAROLINE_WHATSAPP_SIDECAR_DIR).
/// Only two direct dependencies (package.json) -- installing at setup
/// time, same as PythonInstaller does with pip, rather than committing
/// node_modules to git or bundling it at build time.
///
/// Requires NodeInstaller to have already run (uses AppPaths.NodeDir's own
/// npm-cli.js via node.exe, never relies on "npm" being resolvable on PATH,
/// same isolation principle as every other runtime here).
/// </summary>
internal static class WhatsappSidecarInstaller
{
    private static string SidecarDir => Path.Combine(AppPaths.AppDir, "whatsapp-sidecar");
    private static string NodeModulesMarker => Path.Combine(SidecarDir, "node_modules", ".caroline-installed");

    public static async Task InstallAsync(CancellationToken ct)
    {
        if (!Directory.Exists(SidecarDir))
        {
            Logger.Log($"WhatsappSidecarInstaller: {SidecarDir} not found (older build without the sidecar?) -- skipping, WhatsApp just won't be available.");
            return;
        }
        if (File.Exists(NodeModulesMarker))
        {
            Logger.Log("WhatsappSidecarInstaller: already installed, skipping");
            return;
        }

        // npm-cli.js lives under node's own install tree -- invoked via node.exe
        // directly rather than the npm.cmd wrapper, so this never depends on
        // cmd.exe shell resolution.
        var npmCliJs = Path.Combine(AppPaths.NodeDir, "node_modules", "npm", "bin", "npm-cli.js");
        if (!File.Exists(npmCliJs))
        {
            throw new InvalidOperationException($"npm-cli.js not found at {npmCliJs} -- Node.js install looks incomplete.");
        }

        var psi = new ProcessStartInfo
        {
            FileName = AppPaths.NodeExe,
            WorkingDirectory = SidecarDir,
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

        Logger.Log($"WhatsappSidecarInstaller: running npm install in {SidecarDir}");
        using var proc = Process.Start(psi) ?? throw new InvalidOperationException("failed to start node.exe for npm install");
        var stdout = await proc.StandardOutput.ReadToEndAsync(ct);
        var stderr = await proc.StandardError.ReadToEndAsync(ct);
        await proc.WaitForExitAsync(ct);
        Logger.Log($"WhatsappSidecarInstaller: npm install exit={proc.ExitCode}\n{stdout}\n{stderr}");
        if (proc.ExitCode != 0)
        {
            throw new InvalidOperationException($"npm install failed (exit {proc.ExitCode}): {stderr}\n{stdout}");
        }

        Directory.CreateDirectory(Path.GetDirectoryName(NodeModulesMarker)!);
        await File.WriteAllTextAsync(NodeModulesMarker, DateTime.UtcNow.ToString("O"), ct);
        Logger.Log("WhatsappSidecarInstaller: npm install complete");
    }
}

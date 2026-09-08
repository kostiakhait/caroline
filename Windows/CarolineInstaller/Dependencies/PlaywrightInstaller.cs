using System.Diagnostics;

namespace CarolineInstaller.Dependencies;

/// <summary>
/// Ensures the Chromium browser Playwright drives (via the browser MCP
/// server's playwright-core dependency, extracted as part of the Caroline
/// app download) is present. playwright-core itself ships no browser
/// binary -- only "chromium.executablePath()" ever gets downloaded, via its
/// own cli.js, into playwright-core's usual cache location
/// (%LOCALAPPDATA%\ms-playwright). That cache is intentionally NOT
/// isolated under AppPaths.Root: it's playwright-core's own well-known
/// location, and reusing it means a browser already fetched by any other
/// Playwright-based tool on the machine is picked up for free.
///
/// Idempotent: asks chromium.executablePath() first (via the isolated
/// Node.js runtime) and only runs the install if that path doesn't exist
/// yet -- confirmed necessary by hand: MCP/browser/src/daemon.ts calls
/// exactly this executablePath() at startup and fails if it's missing.
/// </summary>
internal static class PlaywrightInstaller
{
    private static string BrowserServerDir => Path.Combine(AppPaths.BackendDir, "mcp-servers", "browser");

    public static async Task<bool> IsInstalledAsync(CancellationToken ct)
    {
        var (code, stdout, _) = await RunNodeAsync(
            ["-e", "console.log(require('playwright-core').chromium.executablePath())"], ct);
        if (code != 0) return false;
        var path = stdout.Trim();
        return path.Length > 0 && File.Exists(path);
    }

    public static async Task InstallAsync(Action<string> onStatus, CancellationToken ct)
    {
        if (await IsInstalledAsync(ct))
        {
            Logger.Log("PlaywrightInstaller: Chromium already installed");
            return;
        }

        onStatus("Downloading Chromium for browser automation…");
        var cliPath = Path.Combine(BrowserServerDir, "node_modules", "playwright-core", "cli.js");
        var (code, stdout, stderr) = await RunNodeAsync([cliPath, "install", "chromium"], ct);
        Logger.Log($"PlaywrightInstaller: install exit={code}\n{stdout}\n{stderr}");
        if (code != 0)
        {
            throw new InvalidOperationException($"Chromium install failed (exit {code}): {stderr}");
        }

        if (!await IsInstalledAsync(ct))
        {
            throw new InvalidOperationException("Chromium install verification failed: executablePath() still doesn't resolve to a real file.");
        }
        Logger.Log("PlaywrightInstaller: Chromium installed");
    }

    private static Task<(int Code, string Stdout, string Stderr)> RunNodeAsync(string[] args, CancellationToken ct)
    {
        var psi = new ProcessStartInfo
        {
            FileName = AppPaths.NodeExe,
            WorkingDirectory = BrowserServerDir,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        foreach (var a in args) psi.ArgumentList.Add(a);

        return RunAsync(psi, ct);
    }

    private static async Task<(int, string, string)> RunAsync(ProcessStartInfo psi, CancellationToken ct)
    {
        using var proc = Process.Start(psi) ?? throw new InvalidOperationException($"Failed to start {psi.FileName}");
        var stdoutTask = proc.StandardOutput.ReadToEndAsync(ct);
        var stderrTask = proc.StandardError.ReadToEndAsync(ct);
        await proc.WaitForExitAsync(ct);
        return (proc.ExitCode, await stdoutTask, await stderrTask);
    }
}

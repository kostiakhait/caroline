using System.Diagnostics;
using System.IO.Compression;
using System.Net.Http;

namespace CarolineInstaller.Dependencies;

/// <summary>
/// Provisions an isolated, embeddable Python runtime under AppPaths.PythonDir
/// -- the official "embeddable package" distribution: no installer, no
/// registry entries, no PATH/PATHEXT changes, nothing that could collide
/// with a Python the machine already has. Used by skills/tools that need
/// Python (e.g. image-processing scripts) without depending on -- or
/// disturbing -- whatever the user has set up for their own work. Also
/// bootstraps pip and installs the packages Caroline's own backend needs
/// (see PythonPackages below) -- the embeddable distribution ships with
/// NEITHER by design (Microsoft's own doc: "pip is not included... you can
/// manually add it").
///
/// Runtime extraction is idempotent (does nothing if AppPaths.PythonExe
/// already exists); the pip/package bootstrap is checked SEPARATELY (see
/// ArePackagesInstalled) so an existing install from before this bootstrap
/// existed still gets it on its next run, instead of being skipped forever
/// just because Python itself was already there -- per explicit instruction
/// (2026-09-07): the exact same "a new capability never reaches an existing
/// install" gap already found and fixed for MCP servers today.
///
/// Mirrored on our own server (see AppPaths.DependencyMirrorBaseUrl) rather
/// than fetched from python.org directly, with the hash pinned here --
/// computed once, from the real python.org release, when this version was
/// mirrored (python.org itself doesn't publish a co-located checksum file
/// the way nodejs.org does, so there was never a live one to fetch anyway).
/// get-pip.py is the one exception fetched live, not mirrored: bootstrap.pypa.io
/// is itself the Python Packaging Authority's own dedicated, permanently-stable
/// URL for exactly this purpose (unlike a GitHub release asset, there's nothing
/// here that could get renamed out from under us).
/// </summary>
internal static class PythonInstaller
{
    private const string Version = "3.12.8";
    private const string ZipFileName = $"python-{Version}-embed-amd64.zip";
    private const string ExpectedSha256 = "8d3f33be9eb810f23c102f08475af2854e50484b8e4e06275e937be61ce3d2fb";
    private const string PthFileName = "python312._pth";
    private const string GetPipUrl = "https://bootstrap.pypa.io/get-pip.py";

    // Packages Caroline's own backend needs, installed once via pip. edge-tts backs
    // the local (non-SquirrelWisdom) text-to-speech path -- see Caroline/backend/src/
    // voice.ts and its local_tts_server.py companion -- added specifically to cut
    // per-call latency (no Camerlengo round trip), not to save money.
    private static readonly string[] PythonPackages = ["edge-tts"];

    public static bool IsInstalled() => File.Exists(AppPaths.PythonExe);

    private static string PipExe => Path.Combine(AppPaths.PythonDir, "Scripts", "pip.exe");

    private static bool ArePackagesInstalled()
    {
        if (!File.Exists(PipExe)) return false;
        return PythonPackages.All(pkg =>
            Directory.Exists(Path.Combine(AppPaths.PythonDir, "Lib", "site-packages", pkg.Replace('-', '_'))));
    }

    public static async Task InstallAsync(Downloader downloader,
        Action<string> onStatus, Action<DownloadProgress> onProgress, CancellationToken ct)
    {
        if (!IsInstalled())
        {
            var zipPath = Path.Combine(AppPaths.Root, ZipFileName);
            onStatus("Downloading Python…");
            await downloader.DownloadAsync($"{AppPaths.DependencyMirrorBaseUrl}/{ZipFileName}", zipPath, ExpectedSha256,
                p => onProgress(p), ct);

            onStatus("Installing Python…");
            await Task.Run(() =>
            {
                if (Directory.Exists(AppPaths.PythonDir)) Directory.Delete(AppPaths.PythonDir, recursive: true);
                Directory.CreateDirectory(AppPaths.PythonDir);
                // The embeddable zip has python.exe at its own root already -- no
                // nested top-level folder to unwrap, unlike Node's dist zip.
                ZipFile.ExtractToDirectory(zipPath, AppPaths.PythonDir);
                File.Delete(zipPath);
            }, ct);

            if (!File.Exists(AppPaths.PythonExe))
            {
                throw new InvalidOperationException($"Python install verification failed: {AppPaths.PythonExe} not found after extracting.");
            }
            Logger.Log($"PythonInstaller: installed to {AppPaths.PythonDir}");
        }
        else
        {
            Logger.Log($"PythonInstaller: already installed at {AppPaths.PythonExe}");
        }

        if (ArePackagesInstalled())
        {
            Logger.Log("PythonInstaller: pip + required packages already installed, skipping");
            return;
        }

        onStatus("Setting up Python packages…");
        EnableSitePackages();
        await BootstrapPipAsync(ct);
        await InstallPackagesAsync(ct);

        if (!ArePackagesInstalled())
        {
            throw new InvalidOperationException("Python package install verification failed: not all required packages found in site-packages after install.");
        }
        Logger.Log("PythonInstaller: pip + required packages installed");
    }

    /// <summary>Embeddable distributions ship with "import site" commented out in their
    /// ._pth file by default (Microsoft's own documented default) -- without uncommenting
    /// it, pip-installed packages under Lib\site-packages are never actually importable,
    /// no matter how correctly they installed.</summary>
    private static void EnableSitePackages()
    {
        var pthPath = Path.Combine(AppPaths.PythonDir, PthFileName);
        if (!File.Exists(pthPath))
        {
            Logger.Log($"PythonInstaller: EnableSitePackages: {pthPath} not found (Python distribution layout may have changed) -- pip/imports may not work");
            return;
        }
        var lines = File.ReadAllLines(pthPath);
        var changed = false;
        for (var i = 0; i < lines.Length; i++)
        {
            if (lines[i].Trim() == "#import site")
            {
                lines[i] = "import site";
                changed = true;
            }
        }
        if (changed)
        {
            File.WriteAllLines(pthPath, lines);
            Logger.Log($"PythonInstaller: EnableSitePackages: uncommented 'import site' in {pthPath}");
        }
        else
        {
            Logger.Log($"PythonInstaller: EnableSitePackages: {pthPath} already has site imports enabled (or line not found in expected form)");
        }
    }

    private static async Task BootstrapPipAsync(CancellationToken ct)
    {
        var getPipPath = Path.Combine(Path.GetTempPath(), $"caroline-get-pip-{Guid.NewGuid():N}.py");
        try
        {
            Logger.Log($"PythonInstaller: downloading {GetPipUrl}");
            using (var http = new HttpClient())
            using (var response = await http.GetAsync(GetPipUrl, ct))
            {
                response.EnsureSuccessStatusCode();
                await File.WriteAllBytesAsync(getPipPath, await response.Content.ReadAsByteArrayAsync(ct), ct);
            }
            await RunPythonAsync([getPipPath, "--no-warn-script-location"], "get-pip.py", ct);
        }
        finally
        {
            try { File.Delete(getPipPath); } catch (Exception ex) { Logger.Log($"PythonInstaller: cleanup of {getPipPath} failed (ignored): {ex.Message}"); }
        }
    }

    private static async Task InstallPackagesAsync(CancellationToken ct)
    {
        foreach (var package in PythonPackages)
        {
            await RunPythonAsync(["-m", "pip", "install", "--no-warn-script-location", package], $"pip install {package}", ct);
        }
    }

    private static async Task RunPythonAsync(string[] args, string label, CancellationToken ct)
    {
        var psi = new ProcessStartInfo
        {
            FileName = AppPaths.PythonExe,
            WorkingDirectory = AppPaths.PythonDir,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        foreach (var arg in args) psi.ArgumentList.Add(arg);

        using var proc = Process.Start(psi) ?? throw new InvalidOperationException($"failed to start python.exe for {label}");
        var stdout = await proc.StandardOutput.ReadToEndAsync(ct);
        var stderr = await proc.StandardError.ReadToEndAsync(ct);
        await proc.WaitForExitAsync(ct);
        Logger.Log($"PythonInstaller: {label} exit={proc.ExitCode}\n{stdout}\n{stderr}");
        if (proc.ExitCode != 0)
        {
            throw new InvalidOperationException($"{label} failed (exit {proc.ExitCode}): {stderr}\n{stdout}");
        }
    }
}

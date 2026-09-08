using System.Diagnostics;

namespace CarolineInstaller.Dependencies;

/// <summary>
/// Provisions an isolated PortableGit distribution under AppPaths.GitDir,
/// giving Caroline's backend a bash.exe to point CLAUDE_CODE_GIT_BASH_PATH
/// at. Without this, a machine with no Git for Windows already installed
/// loses the Bash tool entirely -- confirmed via Claude Code's own docs:
/// Claude Code degrades to a PowerShell-only tool (not a crash), but any
/// skill/instruction that assumes bash syntax silently stops working.
///
/// PortableGit, not the standard Git-for-Windows installer or MinGit:
/// - The standard installer (even pointed at a custom /DIR via
///   /VERYSILENT) triggers a real UAC elevation prompt -- confirmed live,
///   unacceptable for something meant to install invisibly alongside
///   whatever the machine already has.
/// - MinGit deliberately omits bash.exe entirely (git-only, no shell) --
///   the opposite of what's needed.
/// PortableGit is a plain self-extracting archive: no installer, no
/// elevation, no registry/PATH changes at all.
///
/// Extraction syntax is `-o<dir> -y` with NO space between -o and the path
/// (confirmed live: a space there breaks the SFX's argument parsing and it
/// silently fails). Per git-for-windows' own README.portable, a fresh
/// extraction should also run post-install.bat -- but that script hard-
/// codes an optimization step (a hardlink for git.exe's DLLs) against the
/// *default* "C:\Program Files\Git" path regardless of where it's actually
/// run from, so it errors out (permission denied) for any custom
/// extraction directory. Confirmed live this failure is cosmetic: bash.exe
/// and git.exe both work correctly without it, so it's run best-effort
/// (logged, not fatal) rather than treated as a real install failure.
///
/// Idempotent: does nothing if AppPaths.GitBashExe already exists.
///
/// Now mirrored on our own server with a pinned hash (see
/// AppPaths.DependencyMirrorBaseUrl) rather than fetched from GitHub
/// releases directly -- both for hash verification (git-for-windows
/// doesn't publish a co-located checksum file for release assets) and so a
/// renamed/removed GitHub release asset can't silently break every future
/// install.
/// </summary>
internal static class GitBashInstaller
{
    private const string Version = "2.55.0.5";
    private const string ExeFileName = $"PortableGit-{Version}-64-bit.7z.exe";
    // Mirrored on our own server (see AppPaths.DependencyMirrorBaseUrl)
    // rather than fetched from GitHub releases directly, hash pinned here
    // (computed once, from the real GitHub release, when this version was mirrored).
    private const string ExpectedSha256 = "5aa8a20f6e9abb2c755f0e73c91c687701a46b309ad84a0ca6509380fa4ae290";

    public static bool IsInstalled() => File.Exists(AppPaths.GitBashExe);

    public static async Task InstallAsync(Downloader downloader,
        Action<string> onStatus, Action<DownloadProgress> onProgress, CancellationToken ct)
    {
        if (IsInstalled())
        {
            Logger.Log($"GitBashInstaller: already installed at {AppPaths.GitBashExe}");
            return;
        }

        var exePath = Path.Combine(AppPaths.Root, ExeFileName);
        onStatus("Downloading Git Bash (for the Bash tool)…");
        await downloader.DownloadAsync($"{AppPaths.DependencyMirrorBaseUrl}/{ExeFileName}", exePath, ExpectedSha256,
            p => onProgress(p), ct);

        onStatus("Installing Git Bash…");
        if (Directory.Exists(AppPaths.GitDir)) Directory.Delete(AppPaths.GitDir, recursive: true);

        await RunSilentExtractAsync(exePath, AppPaths.GitDir, ct);
        File.Delete(exePath);
        await RunPostInstallBestEffortAsync(AppPaths.GitDir, ct);

        if (!File.Exists(AppPaths.GitBashExe))
        {
            throw new InvalidOperationException($"Git Bash install verification failed: {AppPaths.GitBashExe} not found after extracting.");
        }
        Logger.Log($"GitBashInstaller: installed to {AppPaths.GitDir}");
    }

    private static async Task RunSilentExtractAsync(string exePath, string destDir, CancellationToken ct)
    {
        var psi = new ProcessStartInfo
        {
            FileName = exePath,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        // No space between -o and the path -- confirmed live that a space
        // (e.g. separate ArgumentList entries "-o", destDir, or a quoted
        // "-o \"dir\"") breaks the SFX's own argument parser and it exits
        // 1 without extracting anything. One combined token, exactly
        // "-o<dir>" (.NET's ArgumentList quotes it as a whole if destDir
        // itself contains spaces).
        psi.ArgumentList.Add($"-o{destDir}");
        psi.ArgumentList.Add("-y");

        using var proc = Process.Start(psi) ?? throw new InvalidOperationException($"Failed to start {exePath}");
        var stdout = await proc.StandardOutput.ReadToEndAsync(ct);
        var stderr = await proc.StandardError.ReadToEndAsync(ct);
        await proc.WaitForExitAsync(ct);
        if (proc.ExitCode != 0)
        {
            throw new InvalidOperationException($"PortableGit self-extraction failed (exit {proc.ExitCode}): {stderr}\n{stdout}");
        }
    }

    /// <summary>Best-effort: see the class doc comment for why a nonzero exit here isn't treated as a real failure.</summary>
    private static async Task RunPostInstallBestEffortAsync(string gitDir, CancellationToken ct)
    {
        var scriptPath = Path.Combine(gitDir, "post-install.bat");
        if (!File.Exists(scriptPath)) return;

        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = scriptPath,
                WorkingDirectory = gitDir,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            using var proc = Process.Start(psi) ?? throw new InvalidOperationException("failed to start post-install.bat");
            var stdout = await proc.StandardOutput.ReadToEndAsync(ct);
            var stderr = await proc.StandardError.ReadToEndAsync(ct);
            await proc.WaitForExitAsync(ct);
            Logger.Log($"GitBashInstaller: post-install.bat exit={proc.ExitCode}\n{stdout}\n{stderr}");
        }
        catch (Exception ex)
        {
            Logger.Log($"GitBashInstaller: post-install.bat threw (non-fatal): {ex.Message}");
        }
    }
}

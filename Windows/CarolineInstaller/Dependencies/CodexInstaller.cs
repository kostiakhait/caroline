using System.IO.Compression;

namespace CarolineInstaller.Dependencies;

/// <summary>
/// Downloads the Codex app-server (OpenAI's agent runtime, the engine behind Caroline's
/// "OpenAI" answer source -- see backend-py/app/engines/codex_engine.py) into
/// AppPaths.CodexDir. It is a single self-contained exe; the release ships it zipped.
///
/// Mirrored on our own server with a pinned hash, same convention as every other
/// dependency (see AppPaths.DependencyMirrorBaseUrl). Bumping the version is a
/// deliberate act: upload the new zip to the mirror, then change Version and
/// ExpectedSha256 together.
///
/// Optional feature: unlike Python or Git Bash, a failure here must NOT stop Caroline
/// from installing -- Program.cs treats this step as non-fatal, and the app simply
/// shows OpenAI as unavailable until a later install run succeeds. Idempotent: does
/// nothing if AppPaths.CodexExe already exists (a newer pinned Version replaces it,
/// see VersionMarker).
/// </summary>
internal static class CodexInstaller
{
    // openai/codex release rust-v0.155.1, codex-app-server-x86_64-pc-windows-msvc.exe.zip.
    private const string Version = "0.155.1";
    private const string ZipFileName = $"codex-app-server-{Version}-win-x64.zip";
    private const string ExpectedSha256 = "99f220829a611756f41484839cfd086e32695afdfba5b1b9a2c04433557e7b16";

    private static string VersionMarker => Path.Combine(AppPaths.CodexDir, "version.txt");

    public static bool IsInstalled() =>
        File.Exists(AppPaths.CodexExe) && File.Exists(VersionMarker) && File.ReadAllText(VersionMarker).Trim() == Version;

    public static async Task InstallAsync(Downloader downloader,
        Action<string> onStatus, Action<DownloadProgress> onProgress, CancellationToken ct)
    {
        if (IsInstalled())
        {
            Logger.Log($"CodexInstaller: already installed at {AppPaths.CodexExe}");
            return;
        }

        Directory.CreateDirectory(AppPaths.CodexDir);
        var zipPath = Path.Combine(AppPaths.Root, ZipFileName);
        onStatus("Downloading OpenAI support (Codex)…");
        await downloader.DownloadAsync($"{AppPaths.DependencyMirrorBaseUrl}/{ZipFileName}", zipPath, ExpectedSha256,
            p => onProgress(p), ct);

        onStatus("Installing OpenAI support (Codex)…");
        var tmpExe = AppPaths.CodexExe + ".new";
        await Task.Run(() =>
        {
            using (var zip = ZipFile.OpenRead(zipPath))
            {
                var entry = zip.Entries.FirstOrDefault(e => e.Name.EndsWith(".exe", StringComparison.OrdinalIgnoreCase))
                    ?? throw new InvalidOperationException("Codex zip contains no .exe");
                entry.ExtractToFile(tmpExe, overwrite: true);
            }
            File.Move(tmpExe, AppPaths.CodexExe, overwrite: true);
            File.WriteAllText(VersionMarker, Version);
            File.Delete(zipPath);
        }, ct);

        if (!File.Exists(AppPaths.CodexExe))
        {
            throw new InvalidOperationException($"Codex install verification failed: {AppPaths.CodexExe} not found after extracting.");
        }
        Logger.Log($"CodexInstaller: installed to {AppPaths.CodexExe}");
    }
}

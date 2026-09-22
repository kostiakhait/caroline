using System.Formats.Tar;
using System.IO.Compression;

namespace CarolineInstaller.Dependencies;

/// <summary>
/// Downloads the Codex app-server (OpenAI's agent runtime, the engine behind Caroline's
/// "OpenAI" answer source -- see backend-py/app/engines/codex_engine.py) into
/// AppPaths.CodexDir.
///
/// Ships as the upstream release's own "package" tar.gz, NOT the bare standalone exe --
/// confirmed live (2026-09-22) that codex-app-server.exe alone is not enough: its default
/// "code mode" tool-calling path spawns a SEPARATE sibling process, codex-code-mode-host.exe,
/// only present in this package archive. Missing it doesn't error visibly -- the model just
/// silently narrates a plausible-looking tool result instead of a real one (see the incident
/// this fixes). The package also ships codex-path/rg.exe and codex-resources/*.exe, whatever
/// else Codex's own tools may need; extracted preserving the archive's own relative layout
/// so codex-app-server.exe finds every sibling exactly where it itself expects to find them,
/// rather than us guessing at a flattened one.
///
/// Mirrored on our own server with a pinned hash, same convention as every other dependency
/// (see AppPaths.DependencyMirrorBaseUrl). Bumping the version is a deliberate act: upload the
/// new archive to the mirror, then change Version and ExpectedSha256 together.
///
/// A normal fatal step like every other dependency (see Program.cs) -- nothing installs
/// partially. Idempotent: does nothing if AppPaths.CodexExe already exists at the pinned
/// version (see VersionMarker).
/// </summary>
internal static class CodexInstaller
{
    // openai/codex release rust-v0.155.1, codex-app-server-package-x86_64-pc-windows-msvc.tar.gz.
    private const string Version = "0.155.1";
    private const string ArchiveFileName = $"codex-app-server-package-{Version}-win-x64.tar.gz";
    private const string ExpectedSha256 = "fcb5234b13ca915a68a1de1e4bcdcca1c789da5702f2f281733882608570aee7";

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
        var archivePath = Path.Combine(AppPaths.Root, ArchiveFileName);
        onStatus("Downloading OpenAI support (Codex)…");
        await downloader.DownloadAsync($"{AppPaths.DependencyMirrorBaseUrl}/{ArchiveFileName}", archivePath, ExpectedSha256,
            p => onProgress(p), ct);

        onStatus("Installing OpenAI support (Codex)…");
        var tmpDir = AppPaths.CodexDir + ".new";
        await Task.Run(() =>
        {
            if (Directory.Exists(tmpDir)) Directory.Delete(tmpDir, recursive: true);
            Directory.CreateDirectory(tmpDir);
            using (var fileStream = File.OpenRead(archivePath))
            using (var gzipStream = new GZipStream(fileStream, CompressionMode.Decompress))
            {
                TarFile.ExtractToDirectory(gzipStream, tmpDir, overwriteFiles: true);
            }

            // Move each extracted entry into place individually (not a directory swap --
            // AppPaths.CodexDir may already exist, e.g. left over from a prior failed
            // attempt) so a partial prior state never blocks a clean reinstall.
            foreach (var entry in Directory.GetFileSystemEntries(tmpDir))
            {
                var dest = Path.Combine(AppPaths.CodexDir, Path.GetFileName(entry));
                if (Directory.Exists(dest)) Directory.Delete(dest, recursive: true);
                else if (File.Exists(dest)) File.Delete(dest);
                if (Directory.Exists(entry)) Directory.Move(entry, dest);
                else File.Move(entry, dest);
            }
            Directory.Delete(tmpDir, recursive: true);

            File.WriteAllText(VersionMarker, Version);
            File.Delete(archivePath);
        }, ct);

        if (!File.Exists(AppPaths.CodexExe))
        {
            throw new InvalidOperationException($"Codex install verification failed: {AppPaths.CodexExe} not found after extracting.");
        }
        Logger.Log($"CodexInstaller: installed to {AppPaths.CodexExe}");
    }
}

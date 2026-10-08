using System.Formats.Tar;
using System.IO.Compression;

namespace CarolineInstaller.Dependencies;

/// <summary>
/// Provisions signal-cli (the JVM tool app/signal_channel.py drives as a
/// JSON-RPC daemon over a local TCP socket -- docs/MESSENGER_INTEGRATIONS_
/// PLAN.md, 2026-10-06) under AppPaths.SignalCliDir. Same direct-GitHub-
/// release-asset reasoning as JavaRuntimeInstaller's own doc comment for
/// why this isn't mirrored on our own server. Platform-independent build
/// (needs a JRE, provided separately by JavaRuntimeInstaller) rather than
/// the native/GraalVM build, which has no Windows release at all.
/// </summary>
internal static class SignalCliInstaller
{
    private const string Version = "0.14.9";
    private const string DownloadUrl = $"https://github.com/AsamK/signal-cli/releases/download/v{Version}/signal-cli-{Version}.tar.gz";
    private const string ExpectedSha256 = "c32b87f587198cbd277c9d71228716ee6047ee2b63e8e88728e8280d9407d65a";
    private const string InnerDirName = $"signal-cli-{Version}";

    public static bool IsInstalled() => File.Exists(Path.Combine(AppPaths.SignalCliDir, "bin", "signal-cli.bat"));

    public static async Task InstallAsync(Downloader downloader,
        Action<string> onStatus, Action<DownloadProgress> onProgress, CancellationToken ct)
    {
        if (IsInstalled())
        {
            Logger.Log($"SignalCliInstaller: already installed at {AppPaths.SignalCliDir}");
            return;
        }

        var tarGzPath = Path.Combine(AppPaths.Root, "signal-cli.tar.gz");
        onStatus("Downloading signal-cli…");
        await downloader.DownloadAsync(DownloadUrl, tarGzPath, ExpectedSha256, p => onProgress(p), ct);

        onStatus("Installing signal-cli…");
        var extractDir = Path.Combine(AppPaths.RuntimeDir, "signal-cli-extract-tmp");
        await Task.Run(() =>
        {
            if (Directory.Exists(extractDir)) Directory.Delete(extractDir, recursive: true);
            Directory.CreateDirectory(extractDir);
            using (var fileStream = File.OpenRead(tarGzPath))
            using (var gzipStream = new GZipStream(fileStream, CompressionMode.Decompress))
            {
                TarFile.ExtractToDirectory(gzipStream, extractDir, overwriteFiles: true);
            }
            File.Delete(tarGzPath);

            var innerDir = Path.Combine(extractDir, InnerDirName);
            if (Directory.Exists(AppPaths.SignalCliDir)) Directory.Delete(AppPaths.SignalCliDir, recursive: true);
            Directory.CreateDirectory(AppPaths.RuntimeDir);
            Directory.Move(innerDir, AppPaths.SignalCliDir);
            Directory.Delete(extractDir, recursive: true);
        }, ct);

        if (!IsInstalled())
        {
            throw new InvalidOperationException($"signal-cli install verification failed: bin/signal-cli.bat not found under {AppPaths.SignalCliDir} after extracting.");
        }
        Logger.Log($"SignalCliInstaller: installed signal-cli {Version} to {AppPaths.SignalCliDir}");
    }
}

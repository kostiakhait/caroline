namespace CarolineInstaller.Dependencies;

/// <summary>
/// Downloads a single static ffmpeg.exe into AppPaths.FfmpegDir. Both XcfaRenderer
/// (Visual Mode's audio decode + video encode -- AudioFeatures.LoadPcm, Encoder.Open)
/// and the voice pipeline shell out to "ffmpeg" by name; without this, that resolves
/// via a bare PATH lookup, which works by accident on a dev machine that happens to
/// already have ffmpeg installed and breaks silently (Visual Mode simply never
/// produces anything, with no obvious error) on any machine that doesn't -- confirmed
/// as a real gap, not theoretical.
///
/// A static, single-EXE build (BtbN/FFmpeg-Builds' win64-gpl release, no DLLs to also
/// ship) mirrored on our own server with a pinned hash, same convention as
/// GitBashInstaller -- both for verification (no co-located checksum published
/// upstream) and so a renamed/removed GitHub release asset can't silently break every
/// future install.
///
/// Idempotent: does nothing if AppPaths.FfmpegExe already exists.
/// </summary>
internal static class FfmpegInstaller
{
    private const string FileName = "ffmpeg.exe";
    // n8.1.2, BtbN/FFmpeg-Builds autobuild-2026-09-03-13-17, win64-gpl-8.1 (static, no DLLs).
    private const string ExpectedSha256 = "4c8456a45b33ae51ee73d6ae37ee2fd1d824e2585c0351b11a2e28fad533021e";

    public static bool IsInstalled() => File.Exists(AppPaths.FfmpegExe);

    public static async Task InstallAsync(Downloader downloader,
        Action<string> onStatus, Action<DownloadProgress> onProgress, CancellationToken ct)
    {
        if (IsInstalled())
        {
            Logger.Log($"FfmpegInstaller: already installed at {AppPaths.FfmpegExe}");
            return;
        }

        Directory.CreateDirectory(AppPaths.FfmpegDir);
        onStatus("Downloading ffmpeg (for Visual Mode / voice)…");
        await downloader.DownloadAsync($"{AppPaths.DependencyMirrorBaseUrl}/{FileName}", AppPaths.FfmpegExe, ExpectedSha256,
            p => onProgress(p), ct);

        if (!File.Exists(AppPaths.FfmpegExe))
        {
            throw new InvalidOperationException($"ffmpeg install verification failed: {AppPaths.FfmpegExe} not found after download.");
        }
        Logger.Log($"FfmpegInstaller: installed to {AppPaths.FfmpegExe}");
    }
}

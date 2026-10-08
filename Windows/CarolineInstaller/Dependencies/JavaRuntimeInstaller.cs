using System.IO.Compression;

namespace CarolineInstaller.Dependencies;

/// <summary>
/// Provisions an isolated JRE under AppPaths.JavaDir -- same isolation
/// principle as NodeInstaller/PythonInstaller (never touches/relies on any
/// Java the machine might already have, nothing added to PATH). Needed for
/// app/signal_channel.py's signal-cli daemon (docs/MESSENGER_INTEGRATIONS_
/// PLAN.md, 2026-10-06) -- signal-cli is a JVM tool with no native Windows
/// build suitable for bundling here.
///
/// Fetched directly from the Eclipse Temurin GitHub release (not mirrored
/// on our own server the way Node/Python are) -- deliberately: a GitHub
/// release ASSET under a specific tag is itself immutable/permanent, the
/// same reasoning get-pip.py's own direct-fetch exception already uses in
/// PythonInstaller, unlike python.org/nodejs.org's own download pages
/// (which is why THOSE are mirrored instead). Hash below was computed
/// directly from this exact release asset, same as every other pinned
/// hash in this installer.
/// </summary>
internal static class JavaRuntimeInstaller
{
    private const string Version = "17.0.20.1+1";
    private const string DownloadUrl =
        "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.20.1%2B1/OpenJDK17U-jre_x64_windows_hotspot_17.0.20.1_1.zip";
    private const string ExpectedSha256 = "bc21a93923103cdaac93ee337b0ae4365e739fde36df823dd456bc67c8a9d352";
    // The zip's own single top-level entry -- '+' in the release tag becomes
    // part of this directory name too (confirmed against the real archive).
    private const string InnerDirName = "jdk-17.0.20.1+1-jre";

    public static bool IsInstalled() => File.Exists(AppPaths.JavaExe);

    public static async Task InstallAsync(Downloader downloader,
        Action<string> onStatus, Action<DownloadProgress> onProgress, CancellationToken ct)
    {
        if (IsInstalled())
        {
            Logger.Log($"JavaRuntimeInstaller: already installed at {AppPaths.JavaExe}");
            return;
        }

        var zipPath = Path.Combine(AppPaths.Root, "temurin-jre.zip");
        onStatus("Downloading Java runtime…");
        await downloader.DownloadAsync(DownloadUrl, zipPath, ExpectedSha256, p => onProgress(p), ct);

        onStatus("Installing Java runtime…");
        var extractDir = Path.Combine(AppPaths.RuntimeDir, "java-extract-tmp");
        await Task.Run(() =>
        {
            if (Directory.Exists(extractDir)) Directory.Delete(extractDir, recursive: true);
            ZipFile.ExtractToDirectory(zipPath, extractDir);
            File.Delete(zipPath);

            var innerDir = Path.Combine(extractDir, InnerDirName);
            if (Directory.Exists(AppPaths.JavaDir)) Directory.Delete(AppPaths.JavaDir, recursive: true);
            Directory.CreateDirectory(AppPaths.RuntimeDir);
            Directory.Move(innerDir, AppPaths.JavaDir);
            Directory.Delete(extractDir, recursive: true);
        }, ct);

        if (!File.Exists(AppPaths.JavaExe))
        {
            throw new InvalidOperationException($"Java runtime install verification failed: {AppPaths.JavaExe} not found after extracting.");
        }
        Logger.Log($"JavaRuntimeInstaller: installed Temurin {Version} to {AppPaths.JavaDir}");
    }
}

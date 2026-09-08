using System.IO.Compression;

namespace CarolineInstaller.Dependencies;

/// <summary>
/// Provisions an isolated Node.js runtime under AppPaths.NodeDir -- never
/// added to PATH, never touches (or even looks at) any Node.js the machine
/// might already have. Caroline's own backend and MCP servers are always
/// launched with this exact node.exe (see the app's own BackendProcess,
/// which must be pointed at AppPaths.NodeExe rather than relying on "node"
/// being resolvable on PATH).
///
/// Idempotent: does nothing if AppPaths.NodeExe already exists. No version
/// check beyond that for v1 -- reinstalling to pick up a newer pinned
/// version means deleting runtime\node\ first.
/// </summary>
internal static class NodeInstaller
{
    // Pinned LTS version. Bump deliberately, not automatically -- this
    // installer has no "latest" resolver by design, so every install run
    // (until the pin changes) provisions the exact same, already-tested
    // runtime.
    private const string Version = "22.14.0";
    private const string ZipFileName = $"node-v{Version}-win-x64.zip";
    // Mirrored on our own server (see AppPaths.DependencyMirrorBaseUrl) rather
    // than fetched from nodejs.org directly, with the hash pinned here
    // instead of fetched live from SHASUMS256.txt -- both computed once,
    // from the real nodejs.org release, when this version was mirrored.
    private const string ExpectedSha256 = "55b639295920b219bb2acbcfa00f90393a2789095b7323f79475c9f34795f217";

    public static bool IsInstalled() => File.Exists(AppPaths.NodeExe);

    public static async Task InstallAsync(Downloader downloader,
        Action<string> onStatus, Action<DownloadProgress> onProgress, CancellationToken ct)
    {
        if (IsInstalled())
        {
            Logger.Log($"NodeInstaller: already installed at {AppPaths.NodeExe}");
            return;
        }

        var zipPath = Path.Combine(AppPaths.Root, ZipFileName);
        onStatus("Downloading Node.js…");
        await downloader.DownloadAsync($"{AppPaths.DependencyMirrorBaseUrl}/{ZipFileName}", zipPath, ExpectedSha256,
            p => onProgress(p), ct);

        onStatus("Installing Node.js…");
        var extractDir = Path.Combine(AppPaths.RuntimeDir, "node-extract-tmp");
        await Task.Run(() =>
        {
            if (Directory.Exists(extractDir)) Directory.Delete(extractDir, recursive: true);
            ZipFile.ExtractToDirectory(zipPath, extractDir);
            File.Delete(zipPath);

            // The zip's single top-level entry is "node-v<version>-win-x64\" --
            // move its contents up to be NodeDir itself.
            var innerDir = Path.Combine(extractDir, $"node-v{Version}-win-x64");
            if (Directory.Exists(AppPaths.NodeDir)) Directory.Delete(AppPaths.NodeDir, recursive: true);
            Directory.CreateDirectory(AppPaths.RuntimeDir);
            Directory.Move(innerDir, AppPaths.NodeDir);
            Directory.Delete(extractDir, recursive: true);
        }, ct);

        if (!File.Exists(AppPaths.NodeExe))
        {
            throw new InvalidOperationException($"Node.js install verification failed: {AppPaths.NodeExe} not found after extracting.");
        }
        Logger.Log($"NodeInstaller: installed to {AppPaths.NodeDir}");
    }
}

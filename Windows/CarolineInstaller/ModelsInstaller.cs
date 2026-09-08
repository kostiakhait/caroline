using System.Net.Http;

namespace CarolineInstaller;

/// <summary>
/// Downloads Visual Mode's talking-head models (see ModelsInfo.cs) into
/// AppPaths.ModelsDir, once each -- these are tens of GB per file, so this
/// is the one dependency step here that's expected to take a genuinely long
/// time on a slow connection, and MUST NOT re-run on every install/update
/// once a model is already present and verified. Best-effort: a model that
/// isn't available yet (not deployed, or this specific file 404s) is logged
/// and skipped rather than failing the whole install -- Visual Mode simply
/// stays unavailable for that persona/day until it's deployed, same as
/// resolveVisualModel's own "file doesn't exist -> unavailable" fallback.
/// </summary>
internal static class ModelsInstaller
{
    /// <summary>Per-file marker recording the hash of the last successfully verified download -- mirrors AppPaths.InstalledSha256Path's own reasoning (written only after a verified-good download).</summary>
    private static string InstalledMarkerPath(string fileName) => Path.Combine(AppPaths.ModelsDir, fileName + ".installed-sha256");

    public static async Task InstallAsync(Downloader downloader, HttpClient http,
        Action<string> onStatus, Action<DownloadProgress> onProgress, CancellationToken ct)
    {
        Directory.CreateDirectory(AppPaths.ModelsDir);

        for (var i = 0; i < ModelsInfo.FileNames.Length; i++)
        {
            var fileName = ModelsInfo.FileNames[i];
            onStatus($"Checking talking-head model {fileName} ({i + 1}/{ModelsInfo.FileNames.Length})…");

            var info = await ModelsInfo.FetchAsync(http, fileName, ct);
            if (!info.Available)
            {
                Logger.Log($"ModelsInstaller: {fileName} not available for download, skipping (Visual Mode will just be unavailable for it).");
                continue;
            }

            var destPath = Path.Combine(AppPaths.ModelsDir, fileName);
            var markerPath = InstalledMarkerPath(fileName);
            var alreadyInstalled = File.Exists(destPath) && File.Exists(markerPath)
                && string.Equals((await File.ReadAllTextAsync(markerPath, ct)).Trim(), info.Sha256Hex, StringComparison.OrdinalIgnoreCase);

            if (alreadyInstalled)
            {
                Logger.Log($"ModelsInstaller: {fileName} already installed and verified (sha256={info.Sha256Hex}) -- skipping.");
                continue;
            }

            Logger.Log($"ModelsInstaller: downloading {fileName} (sha256={info.Sha256Hex})...");
            var label = $"Downloading talking-head model {fileName} ({i + 1}/{ModelsInfo.FileNames.Length})…";
            onStatus(label);
            await downloader.DownloadAsync(ModelsInfo.UrlFor(fileName), destPath, info.Sha256Hex, onProgress, ct);
            await File.WriteAllTextAsync(markerPath, info.Sha256Hex, ct);
            Logger.Log($"ModelsInstaller: {fileName} installed and verified.");
        }
    }
}

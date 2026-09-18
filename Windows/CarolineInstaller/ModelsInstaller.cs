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

            // Per explicit instruction (2026-09-18): these are tens of GB
            // EACH -- checked individually here, right before each one
            // downloads, rather than folded into Program.cs's one general
            // up-front check (which would have to assume the worst case,
            // all four, even for someone who'll only ever ask for one
            // persona). Best-effort, matching this whole step's own
            // "not available -> skip" philosophy just above: insufficient
            // space for ONE model must not fail the entire install, since
            // Visual Mode already has a graceful "unavailable" fallback for
            // exactly this (resolveVisualModel's own missing-file case).
            var requiredBytes = await TryGetContentLengthAsync(http, ModelsInfo.UrlFor(fileName), ct);
            if (requiredBytes is long size)
            {
                var available = DiskSpace.GetAvailableFreeBytes(AppPaths.ModelsDir);
                if (available < size)
                {
                    Logger.Log($"ModelsInstaller: not enough free space for {fileName} ({DiskSpace.FormatGb(available)} available, "
                        + $"{DiskSpace.FormatGb(size)} needed) -- skipping it (Visual Mode will just be unavailable for it).");
                    onStatus($"Skipping {fileName}: not enough free disk space.");
                    continue;
                }
            }
            else
            {
                Logger.Log($"ModelsInstaller: couldn't determine {fileName}'s size ahead of time (HEAD request failed) -- downloading without a pre-check.");
            }

            Logger.Log($"ModelsInstaller: downloading {fileName} (sha256={info.Sha256Hex})...");
            var label = $"Downloading talking-head model {fileName} ({i + 1}/{ModelsInfo.FileNames.Length})…";
            onStatus(label);
            await downloader.DownloadAsync(ModelsInfo.UrlFor(fileName), destPath, info.Sha256Hex, onProgress, ct);
            await File.WriteAllTextAsync(markerPath, info.Sha256Hex, ct);
            Logger.Log($"ModelsInstaller: {fileName} installed and verified.");
        }
    }

    /// <summary>Content-Length via a plain HEAD request -- null (not an exception) on any
    /// failure (404, network error, server doesn't return a length), so a caller can fall
    /// back to "download anyway, can't pre-check" rather than treating this as fatal.</summary>
    private static async Task<long?> TryGetContentLengthAsync(HttpClient http, string url, CancellationToken ct)
    {
        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Head, url);
            using var response = await http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct);
            if (!response.IsSuccessStatusCode) return null;
            return response.Content.Headers.ContentLength;
        }
        catch (Exception ex)
        {
            Logger.Log($"ModelsInstaller: HEAD request for {url} failed: {ex.Message}");
            return null;
        }
    }
}

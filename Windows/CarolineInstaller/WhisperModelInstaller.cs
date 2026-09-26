using System.IO.Compression;
using System.Net;
using System.Net.Http;

namespace CarolineInstaller;

/// <summary>
/// Downloads and extracts the optional local speech-recognition model
/// (faster-whisper/CTranslate2, distil-large-v3) into AppPaths.WhisperModelDir
/// -- same plain-directory-with-a-co-located-.sha256 convention as
/// ModelsInfo/ModelsInstaller (Visual Mode's own talking-head models), one
/// level down: apps/caroline/models/whisper-distil-large-v3.zip, extracted
/// (not used as-is -- it's a zip of the model's own file set: model.bin,
/// config.json, tokenizer.json, vocabulary.json, preprocessor_config.json).
///
/// Unconditional (explicit instruction, 2026-09-26: "зашита в инсталлятор
/// сразу") -- every install gets this, regardless of whether local speech
/// recognition is ever turned on in Settings (off by default there). Same
/// best-effort philosophy as ModelsInstaller: not available, low disk
/// space, or a failed download/extract is logged and skipped, never fails
/// the whole install -- local STT just stays unavailable (Settings shows
/// the toggle disabled with a hint) until the next successful install/update.
/// </summary>
internal static class WhisperModelInstaller
{
    private const string FileName = "whisper-distil-large-v3.zip";
    private const string BaseUrl = "https://downloader.multi-portal.org/apps/caroline/models";
    private static string Url => $"{BaseUrl}/{FileName}";

    /// <summary>Marker recording the hash of the last successfully verified+extracted
    /// download -- mirrors ModelsInstaller's own per-file marker reasoning.</summary>
    private static string InstalledMarkerPath => Path.Combine(AppPaths.WhisperModelDir, FileName + ".installed-sha256");

    public static async Task InstallAsync(Downloader downloader, HttpClient http,
        Action<string> onStatus, Action<DownloadProgress> onProgress, CancellationToken ct)
    {
        onStatus("Checking local speech-recognition model…");

        string sha256Hex;
        var shaUrl = Url + ".sha256";
        try
        {
            var body = await HttpRetry.GetStringAsync(http, shaUrl, ct);
            var extracted = ExtractHex64(body);
            if (extracted is null)
            {
                Logger.Log($"WhisperModelInstaller: {shaUrl} missing or malformed -- skipping (local STT will just be unavailable).");
                return;
            }
            sha256Hex = extracted;
        }
        catch (HttpRequestException ex) when (ex.StatusCode == HttpStatusCode.NotFound)
        {
            Logger.Log("WhisperModelInstaller: model not available for download (404) -- skipping.");
            return;
        }
        catch (Exception ex)
        {
            Logger.Log($"WhisperModelInstaller: checksum fetch failed ({ex.Message}) -- skipping.");
            return;
        }

        Directory.CreateDirectory(AppPaths.WhisperModelDir);
        var alreadyInstalled = File.Exists(Path.Combine(AppPaths.WhisperModelDir, "model.bin")) && File.Exists(InstalledMarkerPath)
            && string.Equals((await File.ReadAllTextAsync(InstalledMarkerPath, ct)).Trim(), sha256Hex, StringComparison.OrdinalIgnoreCase);
        if (alreadyInstalled)
        {
            Logger.Log($"WhisperModelInstaller: already installed and verified (sha256={sha256Hex}) -- skipping.");
            return;
        }

        // ~1.5GB -- worth a pre-check, same reasoning as ModelsInstaller's own per-file check.
        var requiredBytes = await TryGetContentLengthAsync(http, Url, ct);
        if (requiredBytes is long size)
        {
            var available = DiskSpace.GetAvailableFreeBytes(AppPaths.WhisperModelDir);
            // Extraction needs roughly another copy's worth of headroom on top of the
            // zip itself (both exist briefly at once) -- require 2.5x, not just 1x.
            if (available < size * 2.5)
            {
                Logger.Log($"WhisperModelInstaller: not enough free space ({DiskSpace.FormatGb(available)} available, "
                    + $"~{DiskSpace.FormatGb((long)(size * 2.5))} needed) -- skipping (local STT will just be unavailable).");
                onStatus("Skipping local speech-recognition model: not enough free disk space.");
                return;
            }
        }
        else
        {
            Logger.Log("WhisperModelInstaller: couldn't determine the model's size ahead of time (HEAD request failed) -- downloading without a pre-check.");
        }

        var zipPath = Path.Combine(Path.GetTempPath(), $"caroline-whisper-model-{Guid.NewGuid():N}.zip");
        try
        {
            Logger.Log($"WhisperModelInstaller: downloading (sha256={sha256Hex})...");
            onStatus("Downloading local speech-recognition model (~1.5GB, one time)…");
            await downloader.DownloadAsync(Url, zipPath, sha256Hex, onProgress, ct);

            onStatus("Installing local speech-recognition model…");
            await Task.Run(() =>
            {
                // Clear out any partial/stale extraction from a previous failed
                // attempt first -- ZipFile.ExtractToDirectory doesn't overwrite
                // existing files by default and would throw on a re-run otherwise.
                if (Directory.Exists(AppPaths.WhisperModelDir)) Directory.Delete(AppPaths.WhisperModelDir, recursive: true);
                Directory.CreateDirectory(AppPaths.WhisperModelDir);
                ZipFile.ExtractToDirectory(zipPath, AppPaths.WhisperModelDir);
            }, ct);

            if (!File.Exists(Path.Combine(AppPaths.WhisperModelDir, "model.bin")))
            {
                Logger.Log("WhisperModelInstaller: model.bin missing after extraction -- treating as failed, local STT will be unavailable.");
                return;
            }
            await File.WriteAllTextAsync(InstalledMarkerPath, sha256Hex, ct);
            Logger.Log("WhisperModelInstaller: installed and verified.");
        }
        catch (Exception ex)
        {
            // Best-effort, same as ModelsInstaller: a failed download/extract must
            // never fail the whole Caroline install -- local STT just stays off.
            Logger.Log($"WhisperModelInstaller: install failed ({ex.Message}) -- skipping (local STT will just be unavailable).");
        }
        finally
        {
            try { File.Delete(zipPath); } catch (Exception ex) { Logger.Log($"WhisperModelInstaller: cleanup of {zipPath} failed (ignored): {ex.Message}"); }
        }
    }

    /// <summary>Takes the leading 64 hex chars (lowercased) from a checksum file body -- same parsing as ModelsInfo's own ExtractHex64 (a plain sync method there, not async, so it can use Span directly; duplicated here rather than shared purely to avoid a cross-file dependency for six lines).</summary>
    private static string? ExtractHex64(string body)
    {
        var span = body.AsSpan().Trim();
        if (span.Length < 64) return null;
        var head = span[..64];
        foreach (var c in head)
        {
            var isHex = c is >= '0' and <= '9' or >= 'a' and <= 'f' or >= 'A' and <= 'F';
            if (!isHex) return null;
        }
        return head.ToString().ToLowerInvariant();
    }

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
            Logger.Log($"WhisperModelInstaller: HEAD request for {url} failed: {ex.Message}");
            return null;
        }
    }
}

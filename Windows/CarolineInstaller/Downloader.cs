using System.Net.Http;
using System.Security.Cryptography;

namespace CarolineInstaller;

public sealed record DownloadProgress(long BytesDownloaded, long TotalBytes, double BytesPerSecond)
{
    public double FractionComplete => TotalBytes <= 0 ? 0 : Math.Clamp((double)BytesDownloaded / TotalBytes, 0, 1);
}

/// <summary>
/// Downloads a single file with progress reporting, optional SHA-256
/// verification, and retry with backoff. Ported from AppleKeyInstaller.
/// Pass <paramref name="expectedSha256Hex"/> as null to skip verification
/// (used for third-party downloads -- Node.js, Python -- whose publishers
/// don't expose a simple co-located checksum file the way our own Caroline
/// build does; see Dependencies/NodeInstaller.cs and PythonInstaller.cs).
/// </summary>
internal sealed class Downloader
{
    private const int MaxAttempts = 5;
    private static readonly TimeSpan MaxBackoff = TimeSpan.FromSeconds(15);
    private static readonly TimeSpan StallTimeout = TimeSpan.FromSeconds(30);

    private readonly HttpClient _http;

    public Downloader(HttpClient http) => _http = http;

    public async Task DownloadAsync(string url, string destPath, string? expectedSha256Hex,
        Action<DownloadProgress> onProgress, CancellationToken ct)
    {
        for (var attempt = 1; ; attempt++)
        {
            try
            {
                await DownloadOnceAsync(url, destPath, expectedSha256Hex, onProgress, ct);
                return;
            }
            catch (Exception ex) when (!ct.IsCancellationRequested
                && ex is IOException or HttpRequestException or InvalidDataException or UnauthorizedAccessException)
            {
                Logger.Log($"Downloader: attempt {attempt}/{MaxAttempts} failed: {ex.GetType().Name}: {ex.Message}");
                if (attempt >= MaxAttempts)
                {
                    throw;
                }
                var backoff = TimeSpan.FromSeconds(Math.Pow(2, attempt));
                await Task.Delay(backoff > MaxBackoff ? MaxBackoff : backoff, ct);
            }
        }
    }

    private async Task DownloadOnceAsync(string url, string destPath, string? expectedSha256Hex,
        Action<DownloadProgress> onProgress, CancellationToken ct)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(destPath)!);
        var tempPath = destPath + ".downloading";

        // Resume from a previous attempt's partial file instead of
        // redownloading from byte 0 -- per explicit instruction (2026-09-05):
        // the retry loop above already covers a transient failure mid-way
        // through a 300MB+ app zip or a 12GB+ model file, but until now every
        // retry threw the partial progress away and started over, which on a
        // slow/flaky connection could mean never actually finishing. Falls
        // back to a clean restart (below) if the server doesn't honor Range.
        var resumeFrom = File.Exists(tempPath) ? new FileInfo(tempPath).Length : 0;

        using var request = new HttpRequestMessage(HttpMethod.Get, url);
        if (resumeFrom > 0) request.Headers.Range = new System.Net.Http.Headers.RangeHeaderValue(resumeFrom, null);

        using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct);
        var resumed = resumeFrom > 0 && response.StatusCode == System.Net.HttpStatusCode.PartialContent;
        if (resumeFrom > 0 && !resumed)
        {
            // Server ignored the Range request (200 instead of 206) -- can't
            // trust the existing partial bytes line up with this response,
            // so start clean rather than risk silently corrupting the file.
            Logger.Log($"Downloader: server didn't honor Range for {url} (status={(int)response.StatusCode}) -- restarting from scratch");
            resumeFrom = 0;
        }
        response.EnsureSuccessStatusCode();
        var contentLength = response.Content.Headers.ContentLength ?? 0;
        var totalBytes = resumed ? resumeFrom + contentLength : contentLength;
        if (resumed) Logger.Log($"Downloader: resuming {url} from byte {resumeFrom}/{totalBytes}");

        await using (var httpStream = await response.Content.ReadAsStreamAsync(ct))
        await using (var fileStream = new FileStream(tempPath, resumed ? FileMode.Append : FileMode.Create, FileAccess.Write))
        {
            using var readCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            var buffer = new byte[81920];
            long downloaded = resumed ? resumeFrom : 0;
            var stopwatch = System.Diagnostics.Stopwatch.StartNew();

            while (true)
            {
                int read;
                try
                {
                    readCts.CancelAfter(StallTimeout);
                    read = await httpStream.ReadAsync(buffer, readCts.Token);
                }
                catch (OperationCanceledException) when (!ct.IsCancellationRequested)
                {
                    throw new IOException($"Connection stalled (no data for {StallTimeout.TotalSeconds:F0}s)");
                }
                if (read == 0)
                {
                    break;
                }

                await fileStream.WriteAsync(buffer.AsMemory(0, read), ct);
                downloaded += read;

                var speed = stopwatch.Elapsed.TotalSeconds > 0 ? (downloaded - (resumed ? resumeFrom : 0)) / stopwatch.Elapsed.TotalSeconds : 0;
                onProgress(new DownloadProgress(downloaded, totalBytes, speed));
            }
        }

        // Hashed as one pass over the fully-assembled file on disk, not
        // incrementally during the read loop -- a resumed download's earlier
        // bytes were written in a PREVIOUS attempt/process invocation, so
        // there's no in-memory incremental hash state to carry forward. A
        // mismatch here (including from a corrupt resume) deletes the temp
        // file, so the next outer-loop attempt naturally starts clean.
        if (expectedSha256Hex is not null)
        {
            using var sha256 = SHA256.Create();
            await using var verifyStream = File.OpenRead(tempPath);
            var actualHash = Convert.ToHexString(await sha256.ComputeHashAsync(verifyStream, ct)).ToLowerInvariant();
            if (!string.Equals(actualHash, expectedSha256Hex, StringComparison.OrdinalIgnoreCase))
            {
                File.Delete(tempPath);
                throw new InvalidDataException($"Hash mismatch: expected {expectedSha256Hex}, got {actualHash}");
            }
        }

        File.Move(tempPath, destPath, overwrite: true);
    }
}

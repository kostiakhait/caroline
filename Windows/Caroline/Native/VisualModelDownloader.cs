using System.IO;
using System.Net;
using System.Net.Http;
using System.Security.Cryptography;
using System.Threading;
using System.Threading.Tasks;
using Caroline.Services;

namespace Caroline.Native;

public sealed record VisualModelDownloadProgress(long BytesDownloaded, long TotalBytes, double BytesPerSecond)
{
    public int Percent => TotalBytes <= 0 ? 0 : (int)System.Math.Clamp(BytesDownloaded * 100.0 / TotalBytes, 0, 100);
}

/// <summary>
/// Downloads one purchased Visual Mode model (.xcfa) into ModelsDir, at
/// RUNTIME -- unlike CarolineInstaller's own ModelsInstaller/Downloader
/// (Windows/CarolineInstaller/), which only ever run at install/update time
/// for the fixed Caroline/Peter set. Triggered by the Python backend's
/// visual_model_download_start WS message (relayed here via chat.js -- see
/// MainWindow.xaml.cs's OnWebMessageReceived) right after a real purchase
/// (app/plugins/visual_models_plugin.py), or flushed at startup for one
/// that couldn't start immediately (main.py's own pending-downloads flush).
///
/// Deliberately a separate, slimmed port of CarolineInstaller/Downloader.cs's
/// resumable-download logic rather than a shared reference -- this app
/// project has never had a ProjectReference to CarolineInstaller (a fully
/// separate deployable exe) and this is small enough that duplicating it
/// here is simpler than introducing that first-ever coupling for one method.
/// </summary>
public static class VisualModelDownloader
{
    private const int MaxAttempts = 5;
    private static readonly TimeSpan MaxBackoff = TimeSpan.FromSeconds(15);
    private static readonly TimeSpan StallTimeout = TimeSpan.FromSeconds(30);

    // Timeout=Infinite deliberately, unlike CarolineInstaller/Program.cs's
    // own 10-minute HttpClient timeout for its Downloader -- HttpClient.
    // Timeout covers the WHOLE request including streaming the response
    // body, so a fixed timeout is wrong for a file that can be tens of GB
    // and take far longer than that on a modest connection. StallTimeout
    // above (a genuine "no bytes arrived recently" detector, re-armed on
    // every chunk read below) is the correct mechanism for catching a
    // truly-dead connection without capping how long a SLOW-but-alive one
    // is allowed to take.
    private static readonly HttpClient Http = new() { Timeout = Timeout.InfiniteTimeSpan };

    public static string ModelsDir => Path.Combine(AppContext.BaseDirectory, "..", "art", "models");

    public static string DestPathFor(string modelName) => Path.Combine(ModelsDir, modelName + ".xcfa");

    /// <summary>Fetches the co-located .sha256 sidecar's first 64 hex chars -- same convention as
    /// CarolineInstaller/ModelsInfo.cs's own ExtractHex64. Null if missing/malformed.</summary>
    public static async Task<string?> FetchSha256Async(string shaUrl, CancellationToken ct)
    {
        try
        {
            var body = await Http.GetStringAsync(shaUrl, ct);
            return ExtractHex64(body);
        }
        catch (HttpRequestException)
        {
            return null;
        }
    }

    /// <summary>Split out as its own non-async method (a ReadOnlySpan&lt;char&gt; local can't
    /// safely cross an `await` boundary in an async method, per C# 12's own ref-struct rules)
    /// -- same parsing as CarolineInstaller/ModelsInfo.cs's own ExtractHex64.</summary>
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

    public static async Task DownloadAsync(string url, string destPath, string? expectedSha256Hex,
        Action<VisualModelDownloadProgress> onProgress, CancellationToken ct)
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
                Logger.Log($"VisualModelDownloader: attempt {attempt}/{MaxAttempts} failed: {ex.GetType().Name}: {ex.Message}");
                if (attempt >= MaxAttempts)
                {
                    throw;
                }
                var backoff = TimeSpan.FromSeconds(Math.Pow(2, attempt));
                await Task.Delay(backoff > MaxBackoff ? MaxBackoff : backoff, ct);
            }
        }
    }

    private static async Task DownloadOnceAsync(string url, string destPath, string? expectedSha256Hex,
        Action<VisualModelDownloadProgress> onProgress, CancellationToken ct)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(destPath)!);
        var tempPath = destPath + ".downloading";

        // Resume from a previous attempt's (or a previous app run's) partial
        // file instead of redownloading tens of GB from byte 0 -- same
        // reasoning as CarolineInstaller/Downloader.cs's own resume logic.
        var resumeFrom = File.Exists(tempPath) ? new FileInfo(tempPath).Length : 0;

        using var request = new HttpRequestMessage(HttpMethod.Get, url);
        if (resumeFrom > 0) request.Headers.Range = new System.Net.Http.Headers.RangeHeaderValue(resumeFrom, null);

        using var response = await Http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct);
        var resumed = resumeFrom > 0 && response.StatusCode == HttpStatusCode.PartialContent;
        if (resumeFrom > 0 && !resumed)
        {
            Logger.Log($"VisualModelDownloader: server didn't honor Range for {url} (status={(int)response.StatusCode}) -- restarting from scratch");
            resumeFrom = 0;
        }
        response.EnsureSuccessStatusCode();
        var contentLength = response.Content.Headers.ContentLength ?? 0;
        var totalBytes = resumed ? resumeFrom + contentLength : contentLength;
        if (resumed) Logger.Log($"VisualModelDownloader: resuming {url} from byte {resumeFrom}/{totalBytes}");

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
                onProgress(new VisualModelDownloadProgress(downloaded, totalBytes, speed));
            }
        }

        // Hashed as one pass over the fully-assembled file on disk, not
        // incrementally during the read loop -- a resumed download's earlier
        // bytes were written in a PREVIOUS attempt/process invocation, so
        // there's no in-memory incremental hash state to carry forward.
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

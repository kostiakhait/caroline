using System.Net;
using System.Net.Http;

namespace CarolineInstaller;

/// <summary>
/// Retry-with-backoff for the installer's small metadata GETs (checksum/
/// version files) -- confirmed live (2026-09-05) that downloader.multi-
/// portal.org intermittently returns 503 (~20% of requests during one
/// observed window, not a one-off blip), and neither DownloadsInfo nor
/// ModelsInfo had any retry at all: a single unlucky 503 on a plain
/// GetStringAsync killed the entire install with a fatal dialog. The actual
/// big-file transfer (Downloader.cs) already retries; this brings the small
/// metadata fetches up to the same standard. 404 is NOT retried -- that's a
/// legitimate "not deployed yet" answer, not a transient failure.
/// </summary>
internal static class HttpRetry
{
    private const int MaxAttempts = 5;
    private static readonly TimeSpan MaxBackoff = TimeSpan.FromSeconds(10);

    public static async Task<string> GetStringAsync(HttpClient http, string url, CancellationToken ct)
    {
        for (var attempt = 1; ; attempt++)
        {
            try
            {
                return await http.GetStringAsync(url, ct);
            }
            catch (HttpRequestException ex) when (ex.StatusCode == HttpStatusCode.NotFound)
            {
                throw; // not transient -- let the caller's existing 404 handling take it
            }
            catch (Exception ex) when (!ct.IsCancellationRequested && ex is HttpRequestException or IOException or TaskCanceledException)
            {
                Logger.Log($"HttpRetry: GET {url} attempt {attempt}/{MaxAttempts} failed: {ex.GetType().Name}: {ex.Message}");
                if (attempt >= MaxAttempts) throw;
                var backoff = TimeSpan.FromSeconds(Math.Pow(2, attempt));
                await Task.Delay(backoff > MaxBackoff ? MaxBackoff : backoff, ct);
            }
        }
    }
}

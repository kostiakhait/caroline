using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Security.Cryptography;

namespace Caroline.Services;

public enum InstallerFetchStatus
{
    /// <summary>Downloaded, and its SHA-256 matches the published one.</summary>
    Verified,
    /// <summary>Downloaded every attempt, but never matched the published SHA-256.</summary>
    Mismatch,
    /// <summary>Downloaded, but the server publishes no checksum to verify against.</summary>
    Unverified,
}

/// <param name="StableMismatch">On Mismatch: every attempt produced the SAME wrong hash. That is not a flaky
/// download (those differ run to run) -- the server's file itself disagrees with its own published checksum,
/// so re-downloading is pointless until something on the server changes.</param>
/// <param name="Fingerprint">What the server looked like when this was attempted (see
/// InstallerFetcher.GetFingerprintAsync) -- compare it later to learn cheaply whether anything changed.</param>
public sealed record InstallerFetchResult(
    InstallerFetchStatus Status, string? Path, string? ExpectedSha256, string? ActualSha256, bool StableMismatch, string Fingerprint);

/// <summary>
/// The download-and-verify half of the self-update, split out of UpdateChecker (which is all WPF: dispatcher,
/// MessageBoxes, shutdown) so it can be exercised on its own, with a fake HttpMessageHandler.
///
/// Per explicit instruction (2026-09-20), after a live incident: a corrupted installer on the download
/// server made this fail three times and end in a modal "failed hash verification after 3 attempts" -- and then
/// nothing, ever. The failure was on the SERVER (same size, different bytes than its published checksum), so
/// retrying at once could never help, but giving up for good was wrong too: the server gets fixed. What matters
/// is telling those cases apart, and being able to ask cheaply "has the server changed since I last failed?"
/// (GetFingerprintAsync: two tiny requests, no 216 MB download) so a background retry only pays for a real
/// download when there is something new to download.
/// </summary>
public sealed class InstallerFetcher
{
    private readonly HttpClient _http;
    private readonly string _installerUrl;
    private readonly string _installerSha256Url;
    private readonly string _tempDir;
    private readonly int _maxAttempts;
    private readonly TimeSpan _resumeDelay;
    private readonly TimeSpan _readIdleTimeout;

    /// <summary>A dropped connection is resumed from where it stopped; the download only gives up after this many
    /// resumes IN A ROW that made no progress at all (a resume that moves the file forward resets the count).</summary>
    private const int MaxResumesWithoutProgress = 5;

    public InstallerFetcher(HttpClient http, string installerUrl, string installerSha256Url, string tempDir, int maxAttempts = 3,
        TimeSpan? resumeDelay = null, TimeSpan? readIdleTimeout = null)
    {
        _http = http;
        _installerUrl = installerUrl;
        _installerSha256Url = installerSha256Url;
        _tempDir = tempDir;
        _maxAttempts = maxAttempts;
        // Flat, never exponential -- same rule as every other retry in Caroline.
        _resumeDelay = resumeDelay ?? TimeSpan.FromSeconds(5);
        _readIdleTimeout = readIdleTimeout ?? TimeSpan.FromSeconds(60);
    }

    /// <summary>Cheap "what does the server have right now" identity: the installer's Content-Length /
    /// Last-Modified / ETag (one HEAD request) plus the published checksum (one tiny GET). Two fingerprints being
    /// equal means the server is byte-for-byte in the same state as before.</summary>
    public async Task<string> GetFingerprintAsync(CancellationToken ct = default)
    {
        string head;
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Head, _installerUrl);
            using var res = await _http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, ct);
            head = $"{(int)res.StatusCode}|{res.Content.Headers.ContentLength}|{res.Content.Headers.LastModified?.UtcTicks}|{res.Headers.ETag?.Tag}";
        }
        catch (HttpRequestException ex) { head = "head-failed:" + ex.GetType().Name; }
        var sha = await FetchSha256Async(ct) ?? "no-sha";
        return head + "|" + sha;
    }

    public async Task<string?> FetchSha256Async(CancellationToken ct = default)
    {
        try
        {
            var body = (await _http.GetStringAsync(_installerSha256Url, ct)).Trim();
            return body.Length >= 64 ? body[..64].ToLowerInvariant() : null;
        }
        catch (HttpRequestException) { return null; }
    }

    /// <summary>Downloads the installer to a fresh unique file and verifies it, retrying up to maxAttempts.
    /// Never throws for a bad hash -- that is a result, not an exception. onPercent (0-100) is reported as the
    /// download proceeds.</summary>
    public async Task<InstallerFetchResult> FetchAsync(Action<int>? onPercent = null, CancellationToken ct = default)
    {
        var fingerprint = await GetFingerprintAsync(ct);
        var expected = await FetchSha256Async(ct);

        Directory.CreateDirectory(_tempDir);
        // Unique per fetch (never a fixed name): a still-running installer from an earlier update holds its own
        // exe open, and Windows would refuse to overwrite it -- see UpdateChecker's own history on this.
        var path = Path.Combine(_tempDir, $"CarolineInstaller-update-{Guid.NewGuid():N}.exe");

        var seen = new List<string>();
        for (var attempt = 1; attempt <= _maxAttempts; attempt++)
        {
            using var sha256 = SHA256.Create();
            await DownloadResumableAsync(path, sha256, onPercent, ct);

            if (expected is null)
                return new InstallerFetchResult(InstallerFetchStatus.Unverified, path, null, null, false, fingerprint);

            var actual = Convert.ToHexString(sha256.Hash!).ToLowerInvariant();
            if (string.Equals(actual, expected, StringComparison.OrdinalIgnoreCase))
                return new InstallerFetchResult(InstallerFetchStatus.Verified, path, expected, actual, false, fingerprint);

            seen.Add(actual);
        }

        try { File.Delete(path); } catch (IOException) { /* best effort */ }
        return new InstallerFetchResult(InstallerFetchStatus.Mismatch, null, expected, seen[^1], seen.Distinct().Count() == 1, fingerprint);
    }

    /// <summary>Per explicit request (2026-09-26), after a live incident: the update download (216 MB) dropped
    /// mid-stream ("unexpected EOF ... transport stream") and, being one plain GET with no resume, every 5-minute
    /// retry started again from byte 0 -- on an unstable link it could never finish. Now a dropped or stalled
    /// connection continues from the last byte written (HTTP Range, guarded by If-Range so a file replaced on the
    /// server mid-download is restarted, not spliced), the running SHA-256 carries on across resumes, and a
    /// connection that goes silent for _readIdleTimeout counts as dropped instead of hanging forever. Gives up
    /// (throws the last error, which UpdateChecker already turns into its background retry) only after
    /// MaxResumesWithoutProgress resumes in a row that moved nothing.</summary>
    private async Task DownloadResumableAsync(string path, SHA256 sha256, Action<int>? onPercent, CancellationToken ct)
    {
        long done = 0;
        long? total = null;
        string? validator = null;
        var lastPercent = -1;
        var fruitlessResumes = 0;

        while (true)
        {
            var doneAtStart = done;
            try
            {
                using var request = new HttpRequestMessage(HttpMethod.Get, _installerUrl);
                if (done > 0)
                {
                    request.Headers.Range = new RangeHeaderValue(done, null);
                    if (validator is not null) request.Headers.TryAddWithoutValidation("If-Range", validator);
                }
                using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct);
                response.EnsureSuccessStatusCode();

                if (done > 0 && response.StatusCode != HttpStatusCode.PartialContent)
                {
                    // The server ignored the Range (or the file changed under If-Range): its 200 is the whole
                    // file again, so start over rather than append it to what is already here.
                    done = 0;
                    sha256.Initialize();
                }
                if (done == 0)
                {
                    total = response.Content.Headers.ContentLength;
                    validator = response.Headers.ETag?.Tag ?? response.Content.Headers.LastModified?.ToString("R");
                }
                else
                {
                    total = response.Content.Headers.ContentRange?.Length ?? total;
                }

                await using var file = new FileStream(path, done == 0 ? FileMode.Create : FileMode.OpenOrCreate, FileAccess.Write);
                if (done > 0) { file.SetLength(done); file.Seek(done, SeekOrigin.Begin); }
                await using var stream = await response.Content.ReadAsStreamAsync(ct);
                var buffer = new byte[81920];
                while (true)
                {
                    using var idle = CancellationTokenSource.CreateLinkedTokenSource(ct);
                    idle.CancelAfter(_readIdleTimeout);
                    int read;
                    try { read = await stream.ReadAsync(buffer, idle.Token); }
                    catch (OperationCanceledException) when (!ct.IsCancellationRequested)
                    {
                        throw new IOException($"the download stalled: no data for {_readIdleTimeout.TotalSeconds:F0}s");
                    }
                    if (read == 0) break;
                    await file.WriteAsync(buffer.AsMemory(0, read), ct);
                    sha256.TransformBlock(buffer, 0, read, null, 0);
                    done += read;
                    if (total is > 0)
                    {
                        var percent = (int)(done * 100 / total.Value);
                        if (percent != lastPercent) { lastPercent = percent; onPercent?.Invoke(percent); }
                    }
                }

                // A clean EOF short of the announced size is a truncated download, not a finished one.
                if (total is > 0 && done < total.Value)
                    throw new IOException($"the connection closed early after {done} of {total} bytes");
                sha256.TransformFinalBlock([], 0, 0);
                return;
            }
            catch (Exception ex) when ((ex is IOException || ex is HttpRequestException) && !ct.IsCancellationRequested)
            {
                fruitlessResumes = done > doneAtStart ? 0 : fruitlessResumes + 1;
                if (fruitlessResumes >= MaxResumesWithoutProgress) throw;
                await Task.Delay(_resumeDelay, ct);
            }
        }
    }
}

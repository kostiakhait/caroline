using System.Net;
using System.Net.Http;

namespace CarolineInstaller;

internal sealed record ModelDownloadInfo(string FileName, bool Available, string Sha256Hex);

/// <summary>
/// Same plain-directory convention as DownloadsInfo (see its own doc comment),
/// one level down: each Visual Mode model lives at
/// .../apps/caroline/models/&lt;FileName&gt; with a co-located .sha256 sibling.
/// Deployed separately from the app zip itself (see Caroline/deploy_models.bat) --
/// these rarely change and are tens of GB each, so they don't ride along with
/// every routine app deploy the way Caroline.zip does.
/// </summary>
internal static class ModelsInfo
{
    private const string BaseUrl = "https://downloader.multi-portal.org/apps/caroline/models";

    /// <summary>
    /// The four models Visual Mode needs: Caroline/Peter, day-parity A/B (see
    /// visualMode.ts's resolveVisualModel -- even day-of-month -> A, odd -> B).
    /// </summary>
    public static readonly string[] FileNames = { "CarolineA.xcfa", "CarolineB.xcfa", "PeterA.xcfa", "PeterB.xcfa" };

    public static string UrlFor(string fileName) => $"{BaseUrl}/{fileName}";

    public static async Task<ModelDownloadInfo> FetchAsync(HttpClient http, string fileName, CancellationToken ct)
    {
        var shaUrl = UrlFor(fileName) + ".sha256";
        try
        {
            var sha = ExtractHex64(await HttpRetry.GetStringAsync(http, shaUrl, ct));
            if (sha is null)
            {
                Logger.Log($"ModelsInfo: {fileName} -- checksum file missing or malformed");
                return new ModelDownloadInfo(fileName, false, "");
            }
            return new ModelDownloadInfo(fileName, true, sha);
        }
        catch (HttpRequestException ex) when (ex.StatusCode == HttpStatusCode.NotFound)
        {
            Logger.Log($"ModelsInfo: {fileName} -- not available (404)");
            return new ModelDownloadInfo(fileName, false, "");
        }
    }

    /// <summary>Takes the leading 64 hex chars (lowercased) from a checksum file body -- same parsing as DownloadsInfo.</summary>
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
}

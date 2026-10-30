using System.Net;
using System.Net.Http;

namespace CarolineInstaller;

internal sealed record WindowsDownloadInfo(bool Available, string Version, string Sha256Hex, string ZipUrl);

/// <summary>
/// Distribution is a plain directory on the multi-portal downloader, same
/// convention as AppleKeyInstaller's DownloadsInfo:
///
///   <see cref="ZipUrl"/>              - Caroline.zip (self-contained app\ tree, see build.bat)
///   <see cref="ZipUrl"/>.sha256       - its lowercase hex SHA-256 (first token)
///   <see cref="ZipUrl"/>.version      - a human-readable version string (optional)
///
/// Nothing is uploaded to this path by the installer itself -- that's a
/// separate, explicit deploy step (see Caroline/build.bat and Ratatosk's
/// deploy.bat for the established pattern). Until that upload happens this
/// fetch will 404 and the installer reports "not available" rather than
/// failing confusingly.
/// </summary>
internal static class DownloadsInfo
{
    private const string BaseUrl = "https://downloader.multi-portal.org/apps/caroline";

    public const string ZipUrl = BaseUrl + "/Caroline.zip";
    private const string Sha256Url = ZipUrl + ".sha256";
    private const string VersionUrl = ZipUrl + ".version";

    /// <summary>
    /// An update started by the running Caroline passes the zip it was told about by the signed release manifest
    /// (<c>--zip-file NAME --zip-sha256 HEX</c>, docs/UPDATE_EVERYWHERE_PLAN.md): then no hash is taken from the unsigned checksum file.
    /// </summary>
    private static WindowsDownloadInfo? FromSignedManifest()
    {
        var args = Environment.GetCommandLineArgs();
        string? Arg(string name)
        {
            var i = Array.FindIndex(args, a => string.Equals(a, name, StringComparison.OrdinalIgnoreCase));
            return i >= 0 && i + 1 < args.Length ? args[i + 1] : null;
        }
        var file = Arg("--zip-file");
        var sha = ExtractHex64(Arg("--zip-sha256") ?? "");
        if (file is null || sha is null || file != Path.GetFileName(file) || file.StartsWith('.')) return null;
        return new WindowsDownloadInfo(true, Path.GetFileNameWithoutExtension(file), sha, BaseUrl + "/" + Uri.EscapeDataString(file));
    }

    public static async Task<WindowsDownloadInfo> FetchAsync(HttpClient http, CancellationToken ct)
    {
        var signed = FromSignedManifest();
        if (signed is not null)
        {
            Logger.Log($"DownloadsInfo: the signed manifest names {signed.ZipUrl} sha256={signed.Sha256Hex[..16]}...");
            return signed;
        }
        Logger.Log($"DownloadsInfo: fetching {Sha256Url}");
        var sha = ExtractHex64(await HttpRetry.GetStringAsync(http, Sha256Url, ct));
        if (sha is null)
        {
            Logger.Log("DownloadsInfo: checksum file missing or malformed");
            return new WindowsDownloadInfo(false, "", "", ZipUrl);
        }

        var version = await TryGetStringAsync(http, VersionUrl, ct) ?? "";
        version = version.Trim();

        Logger.Log($"DownloadsInfo: available version='{version}' sha256={sha[..16]}...");
        return new WindowsDownloadInfo(true, version, sha, ZipUrl);
    }

    private static async Task<string?> TryGetStringAsync(HttpClient http, string url, CancellationToken ct)
    {
        try
        {
            return await HttpRetry.GetStringAsync(http, url, ct);
        }
        catch (HttpRequestException ex) when (ex.StatusCode == HttpStatusCode.NotFound)
        {
            return null;
        }
    }

    /// <summary>Takes the leading 64 hex chars (lowercased) from a checksum file body.</summary>
    private static string? ExtractHex64(string body)
    {
        var span = body.AsSpan().Trim();
        if (span.Length < 64)
        {
            return null;
        }
        var head = span[..64];
        foreach (var c in head)
        {
            var isHex = c is >= '0' and <= '9' or >= 'a' and <= 'f' or >= 'A' and <= 'F';
            if (!isHex)
            {
                return null;
            }
        }
        return head.ToString().ToLowerInvariant();
    }
}

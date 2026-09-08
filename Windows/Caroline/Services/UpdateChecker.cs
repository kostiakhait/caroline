using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Security.Cryptography;
using System.Windows;
using System.Windows.Threading;

namespace Caroline.Services;

/// <summary>
/// Background self-update, modeled on ShortNerdCat's client-side updater
/// (tunnel_cat/snc/core/updater.go): a periodic check comparing the
/// installed build's hash against what's published, a single yes/no
/// restart-to-update dialog per launch, and a silent apply on confirm.
///
/// Unlike ShortNerdCat's Go client, Caroline's install unit is a whole
/// directory tree (app.exe + backend\ + wwwroot\), not one file, so there's
/// no meaningful equivalent of "rename my own currently-executing image" --
/// that's exactly what CarolineInstaller.exe already does safely (it force-
/// kills any running Caroline.exe and confirms the process is gone via
/// Process.WaitForExit before touching AppDir, the .NET built-in equivalent
/// of the named-event/OpenProcess handshake ShortNerdCat's Go code had to
/// hand-roll). So "self-replace" here means: fetch a fresh copy of that
/// already-safe installer and run it invisibly (--silent-update), rather
/// than reimplementing its extraction logic inside the running app.
/// </summary>
public sealed class UpdateChecker
{
    private const string BaseUrl = "https://downloader.multi-portal.org/apps/caroline";
    private const string ZipSha256Url = BaseUrl + "/Caroline.zip.sha256";
    private const string ZipVersionUrl = BaseUrl + "/Caroline.zip.version";
    private const string InstallerUrl = BaseUrl + "/CarolineInstaller.exe";
    private const string InstallerSha256Url = BaseUrl + "/CarolineInstaller.exe.sha256";
    private const int DownloadMaxAttempts = 3;

    private static readonly TimeSpan InitialDelay = TimeSpan.FromMinutes(1);
    private static readonly TimeSpan CheckInterval = TimeSpan.FromMinutes(30);

    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(30) };
    private readonly DispatcherTimer _timer;
    private bool _promptedThisSession;

    /// <summary>Fired as soon as a newer build is found, independent of the once-per-launch
    /// MessageBox prompt below -- lets the tray icon show "Update to {version}" even after the
    /// user dismissed that prompt, so they can still trigger it manually later. Re-fires (with
    /// a possibly newer version) on every periodic check as long as an update remains available.</summary>
    public event Action<string>? UpdateAvailable;

    /// <summary>Fired once the actual download starts (UI should show a one-time popup here --
    /// see MainWindow's subscriber) and then periodically with download progress, so an update
    /// that was previously silent for 2+ minutes (confirmed live, 2026-09-06: a 216MB download
    /// with zero feedback of any kind between "started" and "done") is now visible the whole
    /// time it's running, in every open tab's status bar, not just the tray.</summary>
    public event Action<string>? StatusChanged;

    public UpdateChecker()
    {
        _timer = new DispatcherTimer { Interval = CheckInterval };
        _timer.Tick += async (_, _) => await CheckAsync();
    }

    public void Start()
    {
        Logger.Log($"[UpdateChecker] starting -- first check in {InitialDelay.TotalSeconds:F0}s, then every {CheckInterval.TotalMinutes:F0}min");
        _timer.Start();
        _ = Task.Delay(InitialDelay).ContinueWith(_ => System.Windows.Application.Current?.Dispatcher.InvokeAsync(CheckAsync));
    }

    private async Task CheckAsync()
    {
        Logger.Log("[UpdateChecker] check starting");
        try
        {
            var remoteSha = await FetchRemoteSha256Async();
            if (remoteSha is null)
            {
                Logger.Log("[UpdateChecker] check: no sha256 published (or fetch failed) -- skipping this tick");
                return;
            }

            var localSha = Native.InstallStateStore.Load().InstalledSha256;
            if (string.Equals(localSha, remoteSha, StringComparison.OrdinalIgnoreCase))
            {
                Logger.Log($"[UpdateChecker] check: up to date (sha={remoteSha})");
                return;
            }

            var remoteVersion = await FetchRemoteVersionAsync() ?? remoteSha[..8];
            Logger.Log($"[UpdateChecker] new version available: local={localSha ?? "(none)"} remote={remoteSha} ({remoteVersion})");
            UpdateAvailable?.Invoke(remoteVersion);

            // The interruptive MessageBox is still only offered once per launch (same as
            // before) -- UpdateAvailable above is what keeps the tray's "Update to ..." item
            // around afterward for the user to click whenever they're ready instead.
            if (_promptedThisSession)
            {
                Logger.Log("[UpdateChecker] already prompted this session -- not re-showing the MessageBox, tray item stays updated");
                return;
            }
            await OfferUpdateAsync();
        }
        catch (Exception ex)
        {
            Logger.Log($"[UpdateChecker] check failed: {ex}");
        }
    }

    private async Task<string?> FetchRemoteVersionAsync()
    {
        try
        {
            var body = await _http.GetStringAsync(ZipVersionUrl);
            var trimmed = body.Trim();
            return trimmed.Length > 0 ? trimmed : null;
        }
        catch (HttpRequestException)
        {
            return null;
        }
    }

    /// <summary>Runs the same download-and-relaunch-installer flow as accepting the MessageBox
    /// prompt, but without asking first -- for the tray's "Update to ..." item, where the click
    /// itself is the user's explicit go-ahead. Launches the installer WITHOUT --silent-update
    /// (per explicit correction, 2026-09-03): both call sites into this method are already a
    /// direct, in-the-moment user action (the MessageBox's "Yes", or the tray click itself), so
    /// there's no "surprise interruption" reason left to hide the installer's own progress
    /// window -- the opposite was true: a fully silent, invisible install with no banner and no
    /// feedback of any kind read as "nothing happened" when the user tried to check on it.</summary>
    public async Task UpdateNowAsync()
    {
        Logger.Log("[UpdateChecker] UpdateNowAsync: entered");
        string installerPath;
        try
        {
            installerPath = await DownloadInstallerAsync();
        }
        catch (Exception ex)
        {
            Logger.Log($"[UpdateChecker] failed to download installer: {ex}");
            System.Windows.MessageBox.Show($"Couldn't download the update: {ex.Message}", "Caroline Update",
                System.Windows.MessageBoxButton.OK, System.Windows.MessageBoxImage.Error);
            return;
        }

        Logger.Log($"[UpdateChecker] launching {installerPath} (manual, via tray)");
        Process.Start(new ProcessStartInfo(installerPath) { UseShellExecute = true });
        System.Windows.Application.Current.Shutdown();
    }

    private async Task<string?> FetchRemoteSha256Async() => await FetchSha256Async(ZipSha256Url);

    private async Task<string?> FetchSha256Async(string url)
    {
        try
        {
            var body = await _http.GetStringAsync(url);
            var trimmed = body.Trim();
            return trimmed.Length >= 64 ? trimmed[..64].ToLowerInvariant() : null;
        }
        catch (HttpRequestException)
        {
            return null;
        }
    }

    private async Task OfferUpdateAsync()
    {
        _promptedThisSession = true;
        Logger.Log("[UpdateChecker] showing update-available MessageBox (first prompt this session)");
        var result = System.Windows.MessageBox.Show(
            "A new version of Caroline is available.\n\nRestart now to update?",
            "Caroline Update", System.Windows.MessageBoxButton.YesNo, System.Windows.MessageBoxImage.Information);
        Logger.Log($"[UpdateChecker] update-available MessageBox result: {result}");
        if (result != System.Windows.MessageBoxResult.Yes) return;
        await UpdateNowAsync();
    }

    /// <summary>
    /// Downloads CarolineInstaller-update.exe and verifies it against the
    /// published SHA-256 before returning, retrying on mismatch -- confirmed
    /// live (2026-09-03) as the actual cause of a self-update silently never
    /// relaunching Caroline: the previous version of this method had NO
    /// integrity check at all (unlike CarolineInstaller's own Downloader.cs,
    /// which always verifies Caroline.zip/model downloads), so a truncated
    /// download produced a corrupt single-file self-contained exe that threw
    /// System.IO.FileNotFoundException for WindowsBase.dll (a bundled managed
    /// assembly cut off mid-file) and crashed before writing even its own
    /// first log line -- invisible to the user beyond "nothing happened".
    /// </summary>
    private async Task<string> DownloadInstallerAsync()
    {
        Logger.Log("[UpdateChecker] DownloadInstallerAsync: starting download");
        StatusChanged?.Invoke("Caroline: Update ongoing...");
        var expectedSha256 = await FetchSha256Async(InstallerSha256Url);

        var tempDir = Path.Combine(Path.GetTempPath(), "Caroline");
        Directory.CreateDirectory(tempDir);
        // Best-effort cleanup of earlier attempts' leftovers -- unique filenames (below)
        // mean nothing else ever deletes these. Failures here (a still-running installer
        // holding one open) are exactly what this whole fix is about and are silently
        // skipped, not fatal.
        try
        {
            foreach (var old in Directory.EnumerateFiles(tempDir, "CarolineInstaller-update-*.exe"))
            {
                try { File.Delete(old); }
                catch (Exception ex) { Logger.Log($"[UpdateChecker] DownloadInstallerAsync: cleanup delete of {old} failed, leaving it (not fatal): {ex.Message}"); }
            }
        }
        catch (Exception ex) { Logger.Log($"[UpdateChecker] DownloadInstallerAsync: leftover-cleanup enumeration of {tempDir} failed (not fatal): {ex.Message}"); }
        // Unique per attempt (was a fixed "CarolineInstaller-update.exe") -- confirmed live
        // (2026-09-03) as a real collision: this process shuts down right after launching
        // the downloaded installer, but that installer itself keeps running for a few
        // minutes (stop old client, extract, relaunch). If the NEW Caroline instance it
        // launches checks for updates again (its own 1-minute initial-delay check) and
        // finds a still-newer build already published in that window -- easy to hit during
        // a burst of same-day deploys -- its own download landed on the exact same fixed
        // path the still-running installer's own exe was executing from, which Windows
        // locks against write access: "Couldn't download the update: ... being used by
        // another process." A unique name per attempt makes that collision impossible
        // regardless of how many overlapping update attempts are in flight at once.
        var path = Path.Combine(tempDir, $"CarolineInstaller-update-{Guid.NewGuid():N}.exe");

        for (var attempt = 1; ; attempt++)
        {
            using var sha256 = SHA256.Create();
            using (var response = await _http.GetAsync(InstallerUrl, HttpCompletionOption.ResponseHeadersRead))
            {
                response.EnsureSuccessStatusCode();
                var totalBytes = response.Content.Headers.ContentLength;
                await using (var fileStream = new FileStream(path, FileMode.Create, FileAccess.Write))
                await using (var httpStream = await response.Content.ReadAsStreamAsync())
                {
                    var buffer = new byte[81920];
                    long downloaded = 0;
                    var lastReported = -1;
                    int read;
                    while ((read = await httpStream.ReadAsync(buffer)) != 0)
                    {
                        await fileStream.WriteAsync(buffer.AsMemory(0, read));
                        sha256.TransformBlock(buffer, 0, read, null, 0);
                        downloaded += read;
                        // Reported by percent-changed, not by time/chunk count -- naturally
                        // throttles itself to ~100 updates over the whole download regardless
                        // of file size or connection speed, instead of flooding every tab's
                        // status bar (and the WebView2 IPC channel) once per 80KB chunk.
                        if (totalBytes is > 0)
                        {
                            var percent = (int)(downloaded * 100 / totalBytes.Value);
                            if (percent != lastReported)
                            {
                                lastReported = percent;
                                StatusChanged?.Invoke($"Caroline: Update ongoing... {percent}%");
                            }
                        }
                    }
                    sha256.TransformFinalBlock([], 0, 0);
                }
            }

            if (expectedSha256 is null)
            {
                // No checksum published (shouldn't normally happen once the deploy
                // target is updated to publish one) -- log it but don't block the
                // update entirely on that alone.
                Logger.Log("[UpdateChecker] WARNING: no published sha256 for the installer, skipping verification");
                return path;
            }

            var actualSha256 = Convert.ToHexString(sha256.Hash!).ToLowerInvariant();
            if (string.Equals(actualSha256, expectedSha256, StringComparison.OrdinalIgnoreCase))
            {
                Logger.Log($"[UpdateChecker] DownloadInstallerAsync: download verified ok (attempt {attempt}), path={path}");
                return path;
            }

            Logger.Log($"[UpdateChecker] DownloadInstallerAsync: attempt {attempt}/{DownloadMaxAttempts} hash mismatch " +
                $"(expected {expectedSha256}, got {actualSha256})");
            if (attempt >= DownloadMaxAttempts)
            {
                File.Delete(path);
                throw new InvalidDataException($"Downloaded installer failed hash verification after {DownloadMaxAttempts} attempts.");
            }
        }
    }
}

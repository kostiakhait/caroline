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
    // Flat, never growing (standing rule: no exponential backoff anywhere) -- how often a FAILED
    // update is re-examined in the background. Cheap: it only costs two tiny requests unless the
    // server has actually changed since the last failure (see InstallerFetcher.GetFingerprintAsync).
    private static readonly TimeSpan FailedUpdateRetryInterval = TimeSpan.FromMinutes(5);

    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(30) };
    private readonly InstallerFetcher _fetcher;
    private readonly DispatcherTimer _timer;
    private readonly DispatcherTimer _retryTimer;
    private bool _promptedThisSession;
    // Set when an installer has been downloaded AND verified but not yet launched (the user said
    // "later", or it was fetched by a background retry) -- UpdateNowAsync uses it instead of
    // downloading 216 MB again.
    private string? _verifiedInstallerPath;
    private string? _lastFailedFingerprint;
    private bool _retryRunning;

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
        _fetcher = new InstallerFetcher(_http, InstallerUrl, InstallerSha256Url, Path.Combine(Path.GetTempPath(), "Caroline"), DownloadMaxAttempts);
        _timer = new DispatcherTimer { Interval = CheckInterval };
        _timer.Tick += async (_, _) => await CheckAsync();
        _retryTimer = new DispatcherTimer { Interval = FailedUpdateRetryInterval };
        _retryTimer.Tick += async (_, _) => await RetryFailedUpdateAsync();
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
            if (_verifiedInstallerPath is not null && File.Exists(_verifiedInstallerPath))
            {
                Logger.Log($"[UpdateChecker] UpdateNowAsync: reusing the already-verified installer {_verifiedInstallerPath}");
                installerPath = _verifiedInstallerPath;
            }
            else
            {
                var result = await FetchInstallerAsync();
                if (result.Status == InstallerFetchStatus.Mismatch)
                {
                    NotifyUpdateFailed(result, userInitiated: true);
                    return;
                }
                installerPath = result.Path!;
            }
        }
        catch (Exception ex)
        {
            // Network trouble, a full disk, etc. -- not a verdict on the server. Same treatment: say so,
            // and keep trying in the background rather than leaving the user with a dead end.
            Logger.Log($"[UpdateChecker] failed to download installer: {ex}");
            StartBackgroundRetry(fingerprint: null);
            System.Windows.MessageBox.Show(
                $"The update couldn't be downloaded right now ({ex.Message}).\n\nNothing on your computer was changed. " +
                "Caroline will keep trying automatically every 5 minutes and offer the update again as soon as it works.",
                "Caroline Update", System.Windows.MessageBoxButton.OK, System.Windows.MessageBoxImage.Warning);
            return;
        }

        _retryTimer.Stop();
        Logger.Log($"[UpdateChecker] launching {installerPath} (manual, via tray)");
        Process.Start(new ProcessStartInfo(installerPath) { UseShellExecute = true });
        System.Windows.Application.Current.Shutdown();
    }

    /// <summary>Downloads and verifies the installer (see InstallerFetcher), publishing progress to
    /// StatusChanged. A bad hash comes back as a Mismatch result, not an exception.</summary>
    private async Task<InstallerFetchResult> FetchInstallerAsync()
    {
        Logger.Log("[UpdateChecker] FetchInstallerAsync: starting download");
        StatusChanged?.Invoke("Caroline: Update ongoing...");
        // Best-effort cleanup of earlier attempts' leftovers -- unique filenames mean nothing else ever
        // deletes these; a still-running installer holding one open is skipped, not fatal.
        try
        {
            foreach (var old in Directory.EnumerateFiles(Path.Combine(Path.GetTempPath(), "Caroline"), "CarolineInstaller-update-*.exe"))
            {
                if (old == _verifiedInstallerPath) continue;
                try { File.Delete(old); }
                catch (Exception ex) { Logger.Log($"[UpdateChecker] cleanup delete of {old} failed, leaving it (not fatal): {ex.Message}"); }
            }
        }
        catch (Exception ex) { Logger.Log($"[UpdateChecker] leftover-cleanup enumeration failed (not fatal): {ex.Message}"); }

        var result = await _fetcher.FetchAsync(percent => StatusChanged?.Invoke($"Caroline: Update ongoing... {percent}%"));
        Logger.Log($"[UpdateChecker] FetchInstallerAsync: {result.Status}" +
            (result.Status == InstallerFetchStatus.Mismatch
                ? $" (expected {result.ExpectedSha256}, got {result.ActualSha256}, stable={result.StableMismatch})"
                : $", path={result.Path}"));
        if (result.Status != InstallerFetchStatus.Mismatch) _verifiedInstallerPath = result.Path;
        return result;
    }

    /// <summary>The server's installer doesn't match its own published checksum. Not the user's problem and
    /// not the end of the road: say what actually happened, then keep re-examining in the background (see
    /// StartBackgroundRetry) until the server has a valid file.</summary>
    private void NotifyUpdateFailed(InstallerFetchResult result, bool userInitiated)
    {
        StartBackgroundRetry(result.Fingerprint);
        StatusChanged?.Invoke("Caroline: Update waiting for the server -- retrying every 5 minutes");
        if (!userInitiated) return;
        var why = result.StableMismatch
            ? "The download server is offering a file that doesn't match its own published checksum. That's a problem on the server, not with your connection or this computer."
            : "The download kept arriving damaged, so it couldn't be verified.";
        System.Windows.MessageBox.Show(
            why + "\n\nNothing on your computer was changed. Caroline will keep checking every 5 minutes and offer the update again as soon as a valid one is available.",
            "Caroline Update", System.Windows.MessageBoxButton.OK, System.Windows.MessageBoxImage.Warning);
    }

    private void StartBackgroundRetry(string? fingerprint)
    {
        _lastFailedFingerprint = fingerprint;
        if (!_retryTimer.IsEnabled)
        {
            Logger.Log($"[UpdateChecker] a failed update will be re-examined every {FailedUpdateRetryInterval.TotalMinutes:F0} min until it works");
            _retryTimer.Start();
        }
    }

    /// <summary>Background retry tick after a failed update. Never shows a dialog while it's still failing.
    /// If the server looks exactly as it did at the last failure (same file identity, same published checksum)
    /// there is nothing new to try and no 216 MB download is made; otherwise it downloads, and on success stops
    /// retrying and offers the update again.</summary>
    private async Task RetryFailedUpdateAsync()
    {
        if (_retryRunning) return;
        _retryRunning = true;
        try
        {
            var fingerprint = await _fetcher.GetFingerprintAsync();
            if (_lastFailedFingerprint is not null && fingerprint == _lastFailedFingerprint)
            {
                Logger.Log("[UpdateChecker] retry: the server looks the same as at the last failure -- nothing new to try");
                return;
            }
            var result = await FetchInstallerAsync();
            if (result.Status == InstallerFetchStatus.Mismatch)
            {
                _lastFailedFingerprint = result.Fingerprint;
                return;
            }
            _retryTimer.Stop();
            _lastFailedFingerprint = null;
            StatusChanged?.Invoke("Caroline: Update ready");
            await OfferUpdateAsync();
        }
        catch (Exception ex)
        {
            Logger.Log($"[UpdateChecker] retry attempt failed (will try again in {FailedUpdateRetryInterval.TotalMinutes:F0} min): {ex.Message}");
        }
        finally { _retryRunning = false; }
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
}

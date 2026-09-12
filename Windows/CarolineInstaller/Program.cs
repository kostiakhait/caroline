using System.Diagnostics;
using System.IO.Compression;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Windows;
using CarolineInstaller.Dependencies;
using CarolineInstaller.UI;

namespace CarolineInstaller;

/*
 * Caroline - Windows installer (thin bootstrapper).
 *
 * Downloads the latest Caroline.zip from the multi-portal downloader over
 * HTTPS, verifies it against its published SHA-256, installs it per-user
 * into %LocalAppData%\Caroline\app, provisions the isolated dependencies
 * Caroline's backend needs (Node.js, Python, Playwright's Chromium -- all
 * idempotent, none touching anything the machine already has), wires up
 * autostart and a desktop shortcut, and launches it. No elevation required.
 * Re-run it to update.
 *
 * Structure is a direct, trimmed-down port of AppleKeyInstaller (itself a
 * port of ShortNerdCat's SncInstaller).
 */
internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        // Invoked this way by the running Caroline app itself (see
        // Caroline\Services\UpdateChecker.cs) when it finds a newer version
        // -- no progress banner, no success dialog, so a background update
        // doesn't look like a fresh install to the user. Errors still show
        // a MessageBox (matching ShortNerdCat's "silent success, visible
        // failure" pattern) -- a broken auto-update must never fail
        // completely invisibly.
        var silent = args.Contains("--silent-update", StringComparer.OrdinalIgnoreCase);

        using var http = new HttpClient { Timeout = TimeSpan.FromMinutes(10) };

        var app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };

        var exitCode = 0;
        app.Startup += async (_, _) =>
        {
            Logger.Log($"=== CarolineInstaller starting (silent={silent}) ===");
            try
            {
                await RunAsync(http, app, silent);
                Logger.Log("RunAsync completed normally");
            }
            catch (OperationCanceledException)
            {
                Logger.Log("RunAsync cancelled by user");
                exitCode = 2;
            }
            catch (Exception ex)
            {
                Logger.Log($"RunAsync failed: {ex}");
                // Per explicit instruction (2026-09-12): every failure shows
                // a CODE, not just a bare message -- see InstallerException's
                // own doc comment for why. Anything that reaches here without
                // already being one (a bug in a step nobody wrapped, or a
                // framework-level failure before any step started) still
                // gets a code rather than showing a bare, code-less message.
                var coded = ex as InstallerException ?? new InstallerException(ErrorCodes.Unexpected, ex.Message, ex);
                // Without an owner, this can render BEHIND the progress
                // window -- confirmed live: that window is Topmost="True",
                // and an ownerless MessageBox doesn't automatically stack
                // above a topmost window, so the failure dialog was
                // invisible, hidden under the still-showing progress banner.
                // app.MainWindow is null in --silent-update mode (no window
                // ever shown there), which is fine -- nothing to hide behind.
                MessageBox.Show(app.MainWindow, $"Setup failed: {coded.Formatted}\n\nDetails: {Logger.LogPathForDisplay}", "Caroline Setup",
                    MessageBoxButton.OK, MessageBoxImage.Error);
                exitCode = 1;
            }
            app.Shutdown(exitCode);
        };

        return app.Run();
    }

    // Held for the whole file-touching critical section (Step 0 through the end of
    // extraction) -- confirmed live (2026-09-03) as a real bug, not hypothetical:
    // StopOtherInstallerInstances (below) only fights over OTHER instances at the very
    // start, it grants no exclusivity for everything after. Two instances launched close
    // enough together (e.g. a freshly-relaunched Caroline immediately finding yet another
    // still-newer build published moments later, during a burst of same-day deploys) could
    // both pass that check before either one actually held anything, then race each other
    // for the same AppDir -- "Setup failed: ... being used by another process," the same
    // class of error the extraction retry-loop fix was written for, except THIS time
    // caused by a second live competing writer, which no amount of retrying alone fixes.
    // Same reasoning as ShortNerdCat's own selfReplaceMutexName (tunnel_cat/snc/core/
    // updater.go): the loser backs off entirely rather than fighting the winner for it.
    private const string InstallerRunMutexName = "Global\\CarolineInstallerRun";

    private static async Task RunAsync(HttpClient http, Application app, bool silent)
    {
        using var cts = new CancellationTokenSource();
        var window = new ProgressWindow();
        window.CancelRequested += () => cts.Cancel();
        if (!silent)
        {
            window.Show();
            app.MainWindow = window;
        }

        using var runMutex = new Mutex(initiallyOwned: false, InstallerRunMutexName);
        if (!runMutex.WaitOne(0))
        {
            Logger.Log("RunAsync: another installer instance is already running this exact critical section -- backing off");
            window.SetStatus("An update is already in progress in another window…");
            await Task.Delay(2000, CancellationToken.None);
            window.Close();
            return;
        }
        try
        {
            await RunCriticalSectionAsync(http, window, cts);
        }
        finally
        {
            runMutex.ReleaseMutex();
        }
    }

    private static async Task RunCriticalSectionAsync(HttpClient http, ProgressWindow window, CancellationTokenSource cts)
    {
        var ct = cts.Token;
        var downloader = new Downloader(http);

        // Step 0: neutralise every earlier copy before anything else can lock files.
        // Safe to actually kill these now (unlike a moment ago, before the mutex above)
        // -- this instance is the sole holder of the run mutex, so anything named
        // "CarolineInstaller" still running at this point is genuinely stale/orphaned,
        // not a legitimate concurrent run that just hasn't reached the mutex yet.
        window.SetStatus("Removing previous installation…");
        await WithStepAsync(ErrorCodes.CleanupPreviousInstall, "Removing previous installation", async () =>
        {
            await Task.Run(Autostart.StopOtherInstallerInstances, ct);
            await Task.Run(Autostart.RemovePreInstallerCopies, ct);
        });

        AppPaths.EnsureRootExists();

        // Step 1: isolated runtimes. Node first -- Playwright's install step
        // later needs it, and neither install touches the system otherwise,
        // so order between Node/Python doesn't matter beyond that.
        if (!NodeInstaller.IsInstalled())
        {
            await WithStepAsync(ErrorCodes.NodeInstall, "Installing Node.js", () => NodeInstaller.InstallAsync(downloader,
                s => window.SetStatus(s),
                p => window.SetDownloadProgress("Downloading Node.js…", p),
                ct));
        }
        else
        {
            Logger.Log("Node.js already present, skipping");
        }

        // Always called, not gated behind IsInstalled() -- PythonInstaller checks the
        // runtime and its own required pip packages separately internally, so an
        // existing install that already has the Python runtime but predates a newly
        // added package still gets that package installed here instead of the whole
        // step being skipped just because python.exe already exists.
        await WithStepAsync(ErrorCodes.PythonInstall, "Installing Python", () => PythonInstaller.InstallAsync(downloader,
            s => window.SetStatus(s),
            p => window.SetDownloadProgress("Downloading Python…", p),
            ct));

        if (!GitBashInstaller.IsInstalled())
        {
            await WithStepAsync(ErrorCodes.GitBashInstall, "Installing Git Bash", () => GitBashInstaller.InstallAsync(downloader,
                s => window.SetStatus(s),
                p => window.SetDownloadProgress("Downloading Git Bash…", p),
                ct));
        }
        else
        {
            Logger.Log("Git Bash already present, skipping");
        }

        if (!FfmpegInstaller.IsInstalled())
        {
            await WithStepAsync(ErrorCodes.FfmpegInstall, "Installing ffmpeg", () => FfmpegInstaller.InstallAsync(downloader,
                s => window.SetStatus(s),
                p => window.SetDownloadProgress("Downloading ffmpeg…", p),
                ct));
        }
        else
        {
            Logger.Log("ffmpeg already present, skipping");
        }

        // Caroline's own window is a WebView2 host -- without the Runtime present,
        // the app fails to show a window (or crashes) on first launch, AFTER this
        // installer would otherwise have already reported success. See
        // WebView2Installer's own doc comment.
        await WithStepAsync(ErrorCodes.WebView2Install, "Installing the WebView2 Runtime",
            () => WebView2Installer.InstallAsync(downloader, s => window.SetStatus(s), ct));

        // Step 2: the Caroline app itself.
        window.SetStatus("Checking for the latest version…");
        var info = await WithStepAsync(ErrorCodes.VersionCheck, "Checking for the latest version", async () =>
        {
            var result = await DownloadsInfo.FetchAsync(http, ct);
            if (!result.Available)
            {
                throw new InvalidOperationException("Caroline isn't currently available for download. Please try again later.");
            }
            return result;
        });
        window.SetVersionInfo(info.Version);
        Logger.Log($"Target version: {info.Version}, sha256={info.Sha256Hex}");

        var installState = InstallStateStore.Load();
        InstallStateStore.TryCleanupPending(installState); // sweep whatever a previous run couldn't clean up

        var alreadyInstalled = File.Exists(AppPaths.ClientExe)
            && string.Equals(installState.InstalledSha256, info.Sha256Hex, StringComparison.OrdinalIgnoreCase);

        if (alreadyInstalled)
        {
            Logger.Log($"Already up to date (sha256={info.Sha256Hex}) -- skipping download and extraction entirely.");
            window.SetStatus("Already up to date…");
        }
        else
        {
            window.SetStatus("Downloading Caroline…");
            await WithStepAsync(ErrorCodes.Download, "Downloading Caroline", () => downloader.DownloadAsync(
                DownloadsInfo.ZipUrl, AppPaths.DownloadZipPath, info.Sha256Hex,
                progress => window.SetDownloadProgress("Downloading Caroline…", progress), ct));

            Logger.Log("Stopping any running instance before extraction");
            await Autostart.StopRunningClientAsync();
            await WithStepAsync(ErrorCodes.Extraction, "Installing Caroline", () => ExtractWithRetryAsync(window, info.Sha256Hex, ct));
        }

        // Step 3: Chromium, now that the app's own copy of playwright-core exists on disk.
        window.SetStatus("Setting up browser automation…");
        await WithStepAsync(ErrorCodes.PlaywrightInstall, "Setting up browser automation",
            () => PlaywrightInstaller.InstallAsync(s => window.SetStatus(s), ct));

        // Step 4: Visual Mode's talking-head models -- tens of GB, idempotent (see
        // ModelsInstaller's own doc comment), best-effort (a model that isn't deployed
        // yet is skipped, not a fatal error -- see ModelsInfo.FetchAsync's 404 handling).
        await WithStepAsync(ErrorCodes.ModelsInstall, "Downloading talking-head models", () => ModelsInstaller.InstallAsync(downloader, http,
            s => window.SetStatus(s),
            p => window.SetDownloadProgress("Downloading talking-head models…", p),
            ct));

        window.SetStatus("Creating shortcut…");
        await WithStepAsync(ErrorCodes.ShortcutOrAutostart, "Creating the desktop shortcut", () => Task.Run(ShortcutManager.CreateDesktopShortcut, ct));

        window.SetStatus("Registering autostart…");
        await WithStepAsync(ErrorCodes.ShortcutOrAutostart, "Registering autostart", () => Task.Run(Autostart.Register, ct));

        window.SetStatus("Starting Caroline…");
        var launched = await WithStepAsync(ErrorCodes.Launch, "Starting Caroline", () =>
        {
            Logger.Log($"Launching {AppPaths.ClientExe}");
            var proc = Process.Start(new ProcessStartInfo(AppPaths.ClientExe)
            {
                WorkingDirectory = AppPaths.AppDir,
                UseShellExecute = true,
            });
            if (proc is null)
            {
                throw new InvalidOperationException($"Process.Start returned null for {AppPaths.ClientExe}.");
            }
            return Task.FromResult(proc);
        });

        // Per explicit instruction (2026-09-12): confirm the app actually stayed up,
        // rather than unconditionally reporting success the instant it was merely
        // requested to start -- confirmed as a real, currently-unmonitored gap: a
        // missing WebView2 Runtime, a corrupted extraction, or an AV false-positive
        // quarantining a just-extracted DLL could all make Caroline.exe exit
        // immediately, and this installer would have no way to know unless it
        // actually checks. UseShellExecute=true above (needed so a per-user, non-
        // elevated launch behaves like a normal user double-click) means `launched`
        // isn't necessarily the real Caroline.exe process handle on every Windows
        // version -- Exited/HasExited can throw or lie in that case, so this check
        // is best-effort, not a hard guarantee, and swallows its own errors rather
        // than turning "couldn't verify" into a false failure report.
        try
        {
            await Task.Delay(2500, CancellationToken.None);
            if (launched.HasExited && launched.ExitCode != 0)
            {
                throw new InstallerException(ErrorCodes.LaunchCrashed,
                    $"Caroline started but exited immediately (exit code {launched.ExitCode}). It may be blocked by "
                    + "antivirus, missing a dependency, or the install may be corrupted -- try running Setup again.");
            }
        }
        catch (InstallerException) { throw; }
        catch (Exception ex)
        {
            Logger.Log($"Post-launch liveness check inconclusive (ignored, not treated as failure): {ex.Message}");
        }

        // Brief pause so the user sees "Starting Caroline…" rather than the window vanishing
        // the instant the child process is merely requested to start.
        await Task.Delay(800, CancellationToken.None);
        window.Close();
    }

    /// <summary>
    /// Runs one setup step, tagging any exception it throws with `code` --
    /// see InstallerException's own doc comment. An exception that's
    /// ALREADY an InstallerException (thrown deliberately by the step
    /// itself, e.g. WebView2Installer's own "bootstrapper exit code"
    /// check) passes through unchanged rather than getting double-wrapped.
    /// </summary>
    private static async Task WithStepAsync(string code, string stepDescription, Func<Task> step)
    {
        try
        {
            await step();
        }
        catch (OperationCanceledException) { throw; }
        catch (InstallerException) { throw; }
        catch (Exception ex)
        {
            throw new InstallerException(code, $"{stepDescription}: {ex.Message}", ex);
        }
    }

    private static async Task<T> WithStepAsync<T>(string code, string stepDescription, Func<Task<T>> step)
    {
        try
        {
            return await step();
        }
        catch (OperationCanceledException) { throw; }
        catch (InstallerException) { throw; }
        catch (Exception ex)
        {
            throw new InstallerException(code, $"{stepDescription}: {ex.Message}", ex);
        }
    }

    /// <summary>
    /// Extracts into a brand-new, uniquely-named directory under AppPaths.Root and, once
    /// verified good, switches state.json's ActiveAppDir to it -- replacing the previous
    /// approach of deleting AppDir and re-extracting directly into one fixed "app" folder.
    /// Confirmed live (2026-09-06), TWICE in one day including once after the
    /// graceful-shutdown-retry fix in StopRunningClientAsync had already run and confirmed
    /// the old Caroline process fully gone: "Setup failed: The process cannot access the
    /// file ...\Caroline\app ... used by another process" could still recur, exhausting a
    /// full 10-attempt/~110s retry budget. Restart Manager's own diagnostic found ZERO
    /// files locked in that failure -- meaning the lock was on the AppDir directory NODE
    /// itself (most likely a transient AV/indexer scan of the just-emptied folder), which
    /// a file-based lock check can never see and StopRunningClient can never fix (it isn't
    /// holding anything). A brand new directory name has never been referenced by
    /// anything, so extraction into it -- and everything after, since nothing touches the
    /// previous install until the swap below -- cannot hit this failure mode at all. No
    /// retry loop is needed here any more: the one operation that used to need one
    /// (touching the previously-occupied AppDir) no longer happens on this path.
    /// </summary>
    private static async Task ExtractWithRetryAsync(ProgressWindow window, string newSha256, CancellationToken ct)
    {
        var state = InstallStateStore.Load();
        var previousAppDir = state.ActiveAppDir;
        var newDirName = "app-" + Guid.NewGuid().ToString("N")[..8];
        var newDir = Path.Combine(AppPaths.Root, newDirName);

        try
        {
            await Task.Run(() =>
                ExtractWithProgress(AppPaths.DownloadZipPath, newDir,
                    progress => window.SetDownloadProgress("Installing…", progress), ct), ct);

            if (!File.Exists(Path.Combine(newDir, "Caroline.exe")))
            {
                throw new InvalidOperationException($"Install verification failed: Caroline.exe not found in {newDir} after extracting.");
            }

            // Only flipped once the new build is verified good -- a marker written before
            // that could make a future run wrongly skip a broken install. Nothing above
            // this point has touched the previously active directory at all, so any
            // failure leaves the existing install completely untouched and still active.
            state.ActiveAppDir = newDirName;
            state.InstalledSha256 = newSha256;
            if (previousAppDir != newDirName && !state.PendingDeletion.Contains(previousAppDir))
            {
                state.PendingDeletion.Add(previousAppDir);
            }
            InstallStateStore.Save(state);

            File.Delete(AppPaths.DownloadZipPath);

            // Quick, non-blocking best-effort attempt right away (most of the time the old
            // process's files are already free by now) -- if this one doesn't clear it,
            // TryCleanupPending's next caller (this installer's next run, or Caroline's own
            // periodic retry once it's running) will keep trying for as long as it takes.
            InstallStateStore.TryCleanupPending(state);
        }
        catch
        {
            try { if (Directory.Exists(newDir)) Directory.Delete(newDir, recursive: true); }
            catch (Exception cleanupEx)
            {
                Logger.Log($"ExtractWithRetryAsync: cleanup of half-extracted {newDir} failed (ignored, harmless leftover): {cleanupEx.Message}");
            }
            throw;
        }
    }

    /// <summary>
    /// Extracts entry by entry with byte-level progress, instead of
    /// ZipFile.ExtractToDirectory's single all-or-nothing call -- that one
    /// was blocking for the ~10s+ it takes to unpack a ~300MB archive, with
    /// no progress and (since it ran on the UI thread) the window reporting
    /// itself as "Not Responding" for the whole stretch.
    /// </summary>
    private static void ExtractWithProgress(string zipPath, string destDir, Action<DownloadProgress> onProgress, CancellationToken ct)
    {
        using var archive = ZipFile.OpenRead(zipPath);
        var totalBytes = archive.Entries.Sum(e => e.Length);
        long extracted = 0;
        var stopwatch = System.Diagnostics.Stopwatch.StartNew();

        foreach (var entry in archive.Entries)
        {
            ct.ThrowIfCancellationRequested();
            var destPath = Path.Combine(destDir, entry.FullName);

            if (entry.Name.Length == 0)
            {
                Directory.CreateDirectory(destPath);
                continue;
            }

            Directory.CreateDirectory(Path.GetDirectoryName(destPath)!);
            using (var entryStream = entry.Open())
            using (var outStream = new FileStream(destPath, FileMode.Create, FileAccess.Write))
            {
                entryStream.CopyTo(outStream);
            }

            extracted += entry.Length;
            var speed = stopwatch.Elapsed.TotalSeconds > 0 ? extracted / stopwatch.Elapsed.TotalSeconds : 0;
            onProgress(new DownloadProgress(extracted, totalBytes, speed));
        }
    }
}

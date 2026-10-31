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
        bool owned;
        try
        {
            owned = runMutex.WaitOne(0);
        }
        catch (AbandonedMutexException)
        {
            // The previous installer died holding it: this one owns it now, and its Step 0 cleans up after that one.
            Logger.Log("RunAsync: the previous installer ended without releasing the run lock -- taking it over");
            owned = true;
        }
        if (!owned)
        {
            Logger.Log("RunAsync: another installer instance is already running this exact critical section -- backing off");
            window.SetStatus("An update is already in progress in another window…");
            await Task.Delay(2000, CancellationToken.None);
            window.Close();
            return;
        }
        try
        {
            // A silent update shows its window once the running Caroline is stopped: from then on nothing else is on the screen.
            await RunCriticalSectionAsync(http, window, cts, silent, () =>
            {
                if (window.IsVisible) return;
                window.Show();
                app.MainWindow = window;
            });
        }
        finally
        {
            runMutex.ReleaseMutex();
        }
    }

    // Order (2026-10-31, per explicit instruction, confirmed live with 1.0.1: Caroline closed first and the installer then downloaded
    // 515 MB and ran npm for ~40 s, two minutes with nothing on the screen): everything that does not touch what a running Caroline uses
    // comes FIRST, while she keeps working -- the zip (already downloaded and verified by her, --zip-path), its extraction into a new,
    // not yet active app folder, that folder's npm installs. Only then is she stopped; the switch to the new folder and the launch follow,
    // a few seconds, with the progress window showing even in a silent update.
    // Per explicit instruction (2026-10-31): NOTHING is downloaded after the stop -- every dependency download (runtimes, Python
    // packages, the speech model) is in PrepareDependenciesAsync, before it; after it only local steps run.
    private static async Task RunCriticalSectionAsync(HttpClient http, ProgressWindow window, CancellationTokenSource cts, bool silent, Action showWindow)
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

        // Per explicit instruction (2026-09-18): check free disk space up
        // front, before spending any time downloading anything -- a full
        // drive used to only surface as a confusing late IOException deep
        // inside whichever step happened to run out of room. 6 GB covers
        // the base app (the downloaded zip and its extracted copy exist on
        // disk at the same time, briefly) plus the isolated runtimes
        // (Node/Python/Git Bash/ffmpeg) plus Playwright's Chromium, with
        // real margin -- NOT the talking-head models, which are tens of GB
        // each and checked individually, per-file, right before each one
        // downloads (see ModelsInstaller.cs) since failing the whole
        // install over an optional, best-effort feature would be wrong.
        const long RequiredBaseFreeBytes = 6L * 1024 * 1024 * 1024;
        DiskSpace.RequireFreeSpace(AppPaths.Root, RequiredBaseFreeBytes, "install Caroline");

        // Windows Defender exclusion for Caroline's own folders -- one UAC prompt, once, never
        // in a --silent-update run, never fails the install. Placed before the heavy
        // downloads/extraction so this very install benefits. See DefenderExclusion.cs.
        window.SetStatus("Configuring Windows Defender…");
        await DefenderExclusion.EnsureAsync(silent, ct);

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

        // Everything that downloads happens here, while Caroline still runs (see the order above).
        await PrepareDependenciesAsync(downloader, http, window, ct);

        string? newDirName = null, zipPath = null;
        if (alreadyInstalled)
        {
            Logger.Log($"Already up to date (sha256={info.Sha256Hex}) -- skipping download and extraction entirely.");
            window.SetStatus("Already up to date…");
        }
        else
        {
            zipPath = await ObtainZipAsync(downloader, info, window, ct);
            newDirName = await WithStepAsync(ErrorCodes.Extraction, "Installing Caroline", () => ExtractWithRetryAsync(window, zipPath, ct));
            var newDir = Path.Combine(AppPaths.Root, newDirName);
            try
            {
                await WithStepAsync(ErrorCodes.WhatsappSidecarInstall, "Setting up WhatsApp support", () => WhatsappSidecarInstaller.InstallAsync(ct, newDir));
                await WithStepAsync(ErrorCodes.MessengerSidecarInstall, "Setting up Messenger support", () => MessengerSidecarInstaller.InstallAsync(ct, newDir));
            }
            catch
            {
                TryDeleteUnactivated(newDir);
                throw;
            }
        }

        // Only now the running Caroline stops (see the order above) -- and only for a new version, as before: already installed, she is left alone.
        if (newDirName is not null)
        {
            Logger.Log("Stopping any running instance");
            await Autostart.StopRunningClientAsync();
            showWindow();
            window.SetStatus("Updating Caroline…");
        }

        // Size-cap sweep of workspace\dehydrated\ -- runs on every install/update,
        // silent mode included, no UAC needed (the user's own per-user folder).
        // See WorkspaceCleanup.cs for why this is the one piece of workspace\ the
        // installer touches at all.
        window.SetStatus("Cleaning up old data…");
        await WithStepAsync(ErrorCodes.WorkspaceCleanup, "Cleaning up old data", () => WorkspaceCleanup.RunAsync(ct));

        // Python packages PrepareDependenciesAsync downloaded for the runtime Caroline was running on: installed now, offline.
        await WithStepAsync(ErrorCodes.PythonInstall, "Installing Python", () => PythonInstaller.FinishPreparedAsync(s => window.SetStatus(s), ct));

        if (newDirName is not null)
        {
            await WithStepAsync(ErrorCodes.Extraction, "Installing Caroline", () => Task.Run(() => ActivateAppDir(newDirName, info.Sha256Hex, zipPath!), ct));
        }
        // Put in place after the stop, from what was downloaded before it (never a download here): a newer speech model.
        await WhisperModelInstaller.FinishPreparedAsync(s => window.SetStatus(s), ct);

        // The sidecars of the now active app folder. For a new version they were set up in that folder before the stop (Node is
        // provisioned before it, PrepareDependenciesAsync) -- these find them in place and do nothing.
        await WithStepAsync(ErrorCodes.WhatsappSidecarInstall, "Setting up WhatsApp support", () => WhatsappSidecarInstaller.InstallAsync(ct));
        await WithStepAsync(ErrorCodes.MessengerSidecarInstall, "Setting up Messenger support", () => MessengerSidecarInstaller.InstallAsync(ct));

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
    /// <summary>
    /// Every dependency that has to be downloaded, while Caroline still runs (2026-10-31, per explicit instruction: nothing is
    /// downloaded after she is stopped, she is stopped only for the switch). Each of these installs only what is not there yet --
    /// and what is not there, a running Caroline is not using. The two that can replace something she does use only download here
    /// and finish after the stop, offline: Python packages for her runtime (PythonInstaller.FinishPreparedAsync) and a newer
    /// speech model (WhisperModelInstaller.FinishPreparedAsync). A failure here fails the install before Caroline was touched.
    /// </summary>
    private static async Task PrepareDependenciesAsync(Downloader downloader, HttpClient http, ProgressWindow window, CancellationToken ct)
    {
        // Node first -- the app folder's sidecars (npm) need it.
        await WithStepAsync(ErrorCodes.NodeInstall, "Installing Node.js", () => NodeInstaller.InstallAsync(downloader,
            s => window.SetStatus(s),
            p => window.SetDownloadProgress("Downloading Node.js…", p),
            ct));

        // Always called: PythonInstaller checks the runtime and its required packages separately, so a runtime that predates a
        // newly added package still gets it.
        await WithStepAsync(ErrorCodes.PythonInstall, "Installing Python", () => PythonInstaller.PrepareAsync(downloader,
            s => window.SetStatus(s),
            p => window.SetDownloadProgress("Downloading Python…", p),
            ct));

        await WithStepAsync(ErrorCodes.GitBashInstall, "Installing Git Bash", () => GitBashInstaller.InstallAsync(downloader,
            s => window.SetStatus(s),
            p => window.SetDownloadProgress("Downloading Git Bash…", p),
            ct));

        await WithStepAsync(ErrorCodes.FfmpegInstall, "Installing ffmpeg", () => FfmpegInstaller.InstallAsync(downloader,
            s => window.SetStatus(s),
            p => window.SetDownloadProgress("Downloading ffmpeg…", p),
            ct));

        // Bug fix (2026-09-22), per explicit instruction: a step that fails must stop the whole install with a clear, coded
        // message box -- nothing installs partially, ever, Codex included.
        await WithStepAsync(ErrorCodes.CodexInstall, "Installing OpenAI support", () => CodexInstaller.InstallAsync(downloader,
            s => window.SetStatus(s),
            p => window.SetDownloadProgress("Downloading OpenAI support…", p),
            ct));

        // Caroline's own window is a WebView2 host -- without the Runtime she fails to show a window on first launch.
        await WithStepAsync(ErrorCodes.WebView2Install, "Installing the WebView2 Runtime",
            () => WebView2Installer.InstallAsync(downloader, s => window.SetStatus(s), ct));

        // Java runtime + signal-cli (app/signal_channel.py: Signal has no official bot API, signal-cli is the standard way).
        await WithStepAsync(ErrorCodes.JavaRuntimeInstall, "Setting up Java runtime", () => JavaRuntimeInstaller.InstallAsync(downloader,
            s => window.SetStatus(s),
            p => window.SetDownloadProgress("Setting up Java runtime…", p),
            ct));
        await WithStepAsync(ErrorCodes.SignalCliInstall, "Setting up Signal support", () => SignalCliInstaller.InstallAsync(downloader,
            s => window.SetStatus(s),
            p => window.SetDownloadProgress("Setting up Signal support…", p),
            ct));

        // Local speech-recognition model (~1.5GB, best-effort, unconditional -- explicit instruction, 2026-09-26).
        await WithStepAsync(ErrorCodes.WhisperModelInstall, "Downloading local speech-recognition model", () => WhisperModelInstaller.InstallAsync(downloader, http,
            s => window.SetStatus(s),
            p => window.SetDownloadProgress("Downloading local speech-recognition model…", p),
            ct));
    }

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
    /// Extracts into a brand-new, uniquely-named directory under AppPaths.Root (ActivateAppDir then
    /// switches state.json's ActiveAppDir to it) -- replacing the previous
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
    ///
    /// Split in two (2026-10-31): this extracts and verifies only, returning the new folder's name; ActivateAppDir switches to it.
    /// The extraction now runs while the old Caroline is still working, and only the switch waits for her to stop.
    /// </summary>
    private static async Task<string> ExtractWithRetryAsync(ProgressWindow window, string zipPath, CancellationToken ct)
    {
        var newDirName = "app-" + Guid.NewGuid().ToString("N")[..8];
        var newDir = Path.Combine(AppPaths.Root, newDirName);

        try
        {
            await Task.Run(() =>
                ExtractWithProgress(zipPath, newDir,
                    progress => window.SetDownloadProgress("Installing…", progress), ct), ct);

            if (!File.Exists(Path.Combine(newDir, "Caroline.exe")))
            {
                throw new InvalidOperationException($"Install verification failed: Caroline.exe not found in {newDir} after extracting.");
            }
            return newDirName;
        }
        catch
        {
            TryDeleteUnactivated(newDir);
            throw;
        }
    }

    private static void TryDeleteUnactivated(string newDir)
    {
        try { if (Directory.Exists(newDir)) Directory.Delete(newDir, recursive: true); }
        catch (Exception cleanupEx)
        {
            Logger.Log($"ExtractWithRetryAsync: cleanup of half-installed {newDir} failed (ignored, harmless leftover): {cleanupEx.Message}");
        }
    }

    /// <summary>Makes the extracted, verified folder the active one (state.json), once the old Caroline has stopped.</summary>
    private static void ActivateAppDir(string newDirName, string newSha256, string zipPath)
    {
        var state = InstallStateStore.Load();
        var previousAppDir = state.ActiveAppDir;

        // Only flipped once the new build is verified good -- a marker written before
        // that could make a future run wrongly skip a broken install. Nothing before
        // this point has touched the previously active directory at all, so any
        // failure leaves the existing install completely untouched and still active.
        state.ActiveAppDir = newDirName;
        state.InstalledSha256 = newSha256;
        if (previousAppDir != newDirName && !state.PendingDeletion.Contains(previousAppDir))
        {
            state.PendingDeletion.Add(previousAppDir);
        }
        InstallStateStore.Save(state);

        // Our own download, or the one the running Caroline made (--zip-path): installed, it is not needed any more.
        try { File.Delete(zipPath); }
        catch (Exception ex) { Logger.Log($"ActivateAppDir: could not delete {zipPath} (ignored): {ex.Message}"); }

        // Quick, non-blocking best-effort attempt right away (most of the time the old
        // process's files are already free by now) -- if this one doesn't clear it,
        // TryCleanupPending's next caller (this installer's next run, or Caroline's own
        // periodic retry once it's running) will keep trying for as long as it takes.
        InstallStateStore.TryCleanupPending(state);
    }

    /// <summary>
    /// The verified zip to install: the one the running Caroline already downloaded (--zip-path) when its sha256 is the signed one,
    /// else downloaded here as before.
    /// </summary>
    private static async Task<string> ObtainZipAsync(Downloader downloader, WindowsDownloadInfo info, ProgressWindow window, CancellationToken ct)
    {
        if (info.LocalZip is not null)
        {
            window.SetStatus("Checking the downloaded update…");
            if (File.Exists(info.LocalZip) && await Downloader.HashMatchesAsync(info.LocalZip, info.Sha256Hex, ct))
            {
                Logger.Log($"Using the zip the running Caroline already downloaded: {info.LocalZip}");
                return info.LocalZip;
            }
            Logger.Log($"{info.LocalZip} is missing or its sha256 differs from the signed one -- downloading it here");
        }
        window.SetStatus("Downloading Caroline…");
        await WithStepAsync(ErrorCodes.Download, "Downloading Caroline", () => downloader.DownloadAsync(
            info.ZipUrl, AppPaths.DownloadZipPath, info.Sha256Hex,
            progress => window.SetDownloadProgress("Downloading Caroline…", progress), ct));
        return AppPaths.DownloadZipPath;
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

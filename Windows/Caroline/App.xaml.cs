using System.Diagnostics;
using System.Net.Http;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Threading;
using Caroline.Interop;
using Caroline.Models;
using Caroline.Native;
using Caroline.Services;

namespace Caroline;

public partial class App : System.Windows.Application
{
    private const string SingleInstanceMutexName = "Caroline.SingleInstance.Mutex";

    private Mutex? _singleInstanceMutex;
    private bool _isFirstInstance;
    private SettingsService _settingsService = null!;
    private AppSettings _settings = null!;
    private MainWindow _mainWindow = null!;
    private readonly UpdateChecker _updateChecker = new();
    private readonly DispatcherTimer _staleInstallCleanupTimer = new() { Interval = TimeSpan.FromMinutes(5) };

    private static readonly TimeSpan SplashMinDuration = TimeSpan.FromSeconds(3);
    // Backend startup was confirmed live (2026-08-31) to legitimately take
    // several minutes under heavy system load -- this just needs to be
    // longer than that could ever reasonably be, not an exact bound. Past
    // this, show the main window anyway; its own WebView2 page has its own
    // "reconnecting..." indicator (chat.js's WS retry loop) to fall back on.
    private static readonly TimeSpan SplashMaxWait = TimeSpan.FromMinutes(5);
    private static readonly TimeSpan BackendPollInterval = TimeSpan.FromSeconds(1);

    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        ShutdownMode = ShutdownMode.OnExplicitShutdown;

        // Nothing caught these before -- any unhandled exception anywhere
        // (UI thread, a background Task, another thread entirely) took the
        // whole app down with zero trace of why. Wired first, before
        // anything else can throw.
        InstallGlobalExceptionLogging();

        // Watchdog role of this same exe (see Native/Watchdog.cs): no windows, no backend,
        // just keeps the shell running. Decided before anything shell-specific is touched.
        if (e.Args.Contains("--watchdog"))
        {
            _ = RunWatchdogModeAsync();
            return;
        }

        Logger.Log($"=== Caroline starting{(e.Args.Length > 0 ? " (" + string.Join(' ', e.Args) + ")" : "")} ===");

        _singleInstanceMutex = new Mutex(true, SingleInstanceMutexName, out _isFirstInstance);
        if (!_isFirstInstance)
        {
            System.Windows.MessageBox.Show("Caroline is already running.", "Caroline",
                MessageBoxButton.OK, MessageBoxImage.Information);
            // announce: false -- this process is not the one the watchdog is watching.
            RequestShutdown("second instance started while Caroline is already running", announce: false);
            return;
        }

        CrashJournal.MarkStarted();
        WatchdogClient.Start();

        _settingsService = new SettingsService();
        _settings = _settingsService.Load();

        _mainWindow = new MainWindow(_settings, _settingsService);
        _mainWindow.ExitRequested += (_, _) => RequestShutdown("tray menu: Exit");
        // "Update to ..." stays available from the tray even after the user
        // dismisses the one-time MessageBox prompt (see UpdateChecker) --
        // clicking it is itself the go-ahead, no second confirmation dialog.
        _updateChecker.UpdateAvailable += (version) => _mainWindow.Dispatcher.Invoke(() => _mainWindow.ShowUpdateAvailable(version));
        // Per explicit instruction (2026-09-06): the download itself used to be completely
        // silent (confirmed live -- a 2m17s, 216MB download with zero feedback anywhere) --
        // now broadcast into every open tab's status bar, plus a one-time tray popup (see
        // MainWindow.BroadcastUpdateStatus).
        _updateChecker.StatusChanged += (text) => _mainWindow.Dispatcher.Invoke(() => _mainWindow.BroadcastUpdateStatus(text));
        // Per explicit correction (2026-09-03): remove the item the moment it's clicked --
        // an update is now in flight and has its own progress window (see UpdateNowAsync's
        // showBanner:true below), the tray item has nothing left to offer, and leaving it
        // clickable risked double-launching the installer.
        _mainWindow.UpdateRequested += async (_, _) =>
        {
            Logger.Log("[App] UpdateRequested received from tray -- hiding tray item, starting update now");
            _mainWindow.HideUpdateAvailable();
            await _updateChecker.UpdateNowAsync();
        };
        // Must Show() here, not stay hidden: a WPF Window has no HWND until
        // first shown, and both the global hotkey (registered in
        // OnSourceInitialized) and the backend/WebView2 startup (in Loaded)
        // depend on that HWND existing -- staying hidden at launch meant
        // neither the hotkey nor the backend ever started.
        _mainWindow.Show();
        // Bug fix (2026-10-04), per explicit instruction: Opacity=0 (the
        // previous approach here) does NOT actually hide WebView2's own
        // content -- WebView2 is a native windowed child HWND, outside
        // WPF's own visual tree/compositor ("airspace"), so it keeps
        // painting its own page regardless of the PARENT WPF Window's
        // Opacity. Confirmed live: exactly the half-loaded/blank-tab
        // flash the 2026-09-13 fix below describes could still show through
        // the "invisible" window the whole time the splash was up. Hide()
        // (Visibility.Hidden) actually unmaps the window at the Win32 level
        // (ShowWindow SW_HIDE) -- including every child HWND, WebView2
        // included -- without destroying it: Loaded/OnSourceInitialized
        // have already fired by this point (both tied to Show() above, not
        // to the window currently being visible), so the hotkey/backend-
        // startup pipeline this comment used to worry about is untouched.
        _mainWindow.Hide();
        var diagSw = Stopwatch.StartNew();
        Logger.Log($"[DIAG] main window Hide() done, IsVisible={_mainWindow.IsVisible}");
        // Per explicit instruction (2026-09-13): while the splash is up, the
        // dialog window must not be visible at all -- confirmed live as a
        // real bug (2026-09-13): a startup forced-compaction on a large tab
        // session made SyncTabListToBackend keep timing out, so the tab
        // strip never populated and the user saw a half-loaded, effectively
        // broken-looking window (only the default tab, blank content) the
        // whole time the splash was supposedly "covering" it. The splash
        // was never actually opaque/full-window (see SplashWindow's own doc
        // comment) -- it's a small floating banner over an already-visible
        // main window, which is fine ONLY once that window's content is
        // real. This supersedes the 2026-09-03 correction below for
        // specifically the loading window; that correction's actual point
        // (don't disable a window the user can already see and use) still
        // holds once the window is shown again further down.
        //
        // Per that 2026-09-03 correction: once visible, the main window must
        // stay fully usable while the backend connects -- the tab strip,
        // menu, everything. Only the chat input field itself should be
        // disabled during that window (see chat.js's connection-status
        // handling), NOT the whole UI. A previous version of this code
        // disabled the entire window here and justified it in this comment
        // as something the user had asked for -- they hadn't; that was
        // fabricated. Do not reintroduce whole-window blocking once shown.

        // Floating transparent splash (cycling photos + "Connecting...", no chrome
        // -- see SplashWindow) is purely a cosmetic loading indicator now, not a
        // blocking overlay -- it does not disable or cover the main window's own
        // tab strip/content. Owner = _mainWindow (not Topmost=True) so it only
        // stacks above ITS OWN owner, not every other window on the desktop.
        var splash = new SplashWindow { Owner = _mainWindow };
        var dismissedEarly = new TaskCompletionSource();
        splash.Closed += (_, _) => dismissedEarly.TrySetResult();
        splash.Show();

        Logger.Log($"[DIAG] before WaitForSplashDismissAsync, elapsed since Hide()={diagSw.Elapsed.TotalSeconds:F1}s");
        await WaitForSplashDismissAsync(dismissedEarly.Task);
        Logger.Log($"[DIAG] after WaitForSplashDismissAsync, elapsed={diagSw.Elapsed.TotalSeconds:F1}s, dismissedEarly={dismissedEarly.Task.IsCompleted}, IsVisible={_mainWindow.IsVisible}, Opacity={_mainWindow.Opacity}");
        if (splash.IsLoaded) splash.Close();
        // The start-up wizard comes first: the chat window is not shown until the wizard is done (or put off with
        // "Finish later"). Nothing to wait for when the backend says no wizard is needed.
        await RunWizardIfNeededAsync();
        _mainWindow.Show();
        Logger.Log($"[DIAG] main window Show() done, IsVisible={_mainWindow.IsVisible}");
        _ = _mainWindow.StartTabsAsync();

        _mainWindow.Activate();
        // Activate() alone can silently no-op here: Windows' foreground-lock
        // rules can block a just-launched process from stealing focus from
        // whatever else is in front (confirmed: launched right after
        // CarolineInstaller's own window closes itself, Caroline's window
        // came up not focused/behind other windows despite Show()
        // succeeding). SetForegroundWindow is more forceful and reliably wins.
        NativeMethods.SetForegroundWindow(new WindowInteropHelper(_mainWindow).Handle);

        _updateChecker.Start();

        // The watchdog brought us back after an exit nobody asked for: say so once.
        if (e.Args.Contains("--restarted") && CrashJournal.LatestWithin(TimeSpan.FromMinutes(2)) is { } crash)
            _mainWindow.ShowRestartedNotice(
                $"Caroline stopped unexpectedly at {crash.Time:HH:mm} and was restarted. Details: {CrashJournal.JournalPath}");

        // Per explicit instruction (2026-09-06): the installer can only try so many times
        // to remove a just-replaced install directory before it has to exit -- this
        // process lives for hours/days and can keep retrying for as long as it takes for
        // whatever transient lock is holding it (an AV/indexer scan, typically) to clear.
        // One attempt now (most locks are already gone by the time Caroline is up and
        // running), then every 5 minutes for as long as this process is alive.
        _ = Task.Run(StaleInstallCleanup.TryCleanupPending);
        _staleInstallCleanupTimer.Tick += (_, _) => _ = Task.Run(StaleInstallCleanup.TryCleanupPending);
        _staleInstallCleanupTimer.Start();
    }

    /// <summary>
    /// Keeps the splash up (and, per OnStartup's Hide()/Show() dance, the main
    /// window invisible) until the backend actually answers GET /api/status
    /// (started by MainWindow.OnLoaded, already running by the time this is
    /// called).
    ///
    /// Issue #12 (2026-10-24, the user's choice: "Только бэкенд"): this also
    /// used to wait for no tab in tabs[] reporting forcedCompactionPending --
    /// a check that could never fire: the tabs (and their sessions, whose
    /// startup compaction it meant) are created only AFTER this returns
    /// (StartTabsAsync, below the Show()), so tabs[] here is always empty.
    /// Removed rather than kept as a false promise; a tab's startup
    /// compaction runs with the window visible, as it always actually did.
    /// Honors SplashMinDuration as a floor (so a warm start still gets a
    /// brief, deliberate branding beat instead of flashing by instantly) and
    /// SplashMaxWait as a ceiling (so a genuinely broken backend doesn't
    /// leave the user staring at a splash forever -- MainWindow's own health
    /// watchdog/restart logic takes over after this either way). An early
    /// click (dismissedEarly) wins over both.
    /// </summary>
    private static async Task WaitForSplashDismissAsync(Task dismissedEarly)
    {
        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(3) };
        var deadline = DateTime.UtcNow + SplashMaxWait;
        var minDeadline = DateTime.UtcNow + SplashMinDuration;

        while (DateTime.UtcNow < deadline)
        {
            if (dismissedEarly.IsCompleted) return;

            bool healthy;
            try
            {
                using var resp = await http.GetAsync($"http://127.0.0.1:{BackendProcess.Port}/api/status");
                healthy = resp.IsSuccessStatusCode;
            }
            catch
            {
                healthy = false;
            }

            if (healthy && DateTime.UtcNow >= minDeadline) return;
            if (!healthy && DateTime.UtcNow < minDeadline)
            {
                // Still within the floor -- just wait out the rest of it,
                // no need to keep polling every second for this part.
                var remaining = minDeadline - DateTime.UtcNow;
                if (remaining > TimeSpan.Zero) await Task.WhenAny(Task.Delay(remaining), dismissedEarly);
                continue;
            }

            await Task.WhenAny(Task.Delay(BackendPollInterval), dismissedEarly);
        }
    }

    private WizardWindow? _wizardWindow;
    private TaskCompletionSource _wizardClosed = new();

    /// <summary>
    /// The start-up wizard (WizardWindow) comes before the chat window, every time the chat window would be shown while
    /// the wizard is not finished: at start, from the tray, from the hotkey. Returns true when the wizard is on screen
    /// (opened now, or brought to the front) -- the caller then must NOT show the chat window; false when no wizard is
    /// needed. A backend that does not answer means no wizard this time -- never a broken start.
    /// </summary>
    public async Task<bool> TryShowWizardAsync()
    {
        if (_wizardWindow != null)
        {
            _wizardWindow.Activate();
            return true;
        }
        try
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(20) };
            var json = await http.GetStringAsync($"http://127.0.0.1:{BackendProcess.Port}/api/wizard/state");
            using var doc = JsonDocument.Parse(json);
            if (!doc.RootElement.TryGetProperty("needed", out var needed) || needed.ValueKind != JsonValueKind.True) return false;
        }
        catch (Exception ex)
        {
            Logger.Log($"wizard: state check failed: {ex.Message}");
            return false;
        }

        if (_wizardWindow != null) return true;   // opened by another caller while the state was being read
        _wizardClosed = new TaskCompletionSource();
        _wizardWindow = new WizardWindow(BackendProcess.Port);
        _wizardWindow.Closed += (_, _) =>
        {
            _wizardWindow = null;
            _wizardClosed.TrySetResult();
        };
        _wizardWindow.Show();
        _wizardWindow.Activate();
        return true;
    }

    /// <summary>At start: shows the wizard when it is needed and returns once its window is closed.</summary>
    private async Task RunWizardIfNeededAsync()
    {
        if (await TryShowWizardAsync()) await _wizardClosed.Task;
    }

    /// <summary>
    /// UI-thread exceptions are marked Handled (logged, app keeps running --
    /// a desktop assistant staying up in a possibly-degraded state beats
    /// vanishing with zero trace). Non-UI-thread exceptions can't be saved
    /// this way (the CLR terminates the process regardless once
    /// AppDomain.UnhandledException fires) -- logging there is purely so
    /// there's something in caroline.log to look at afterward.
    /// </summary>
    private void InstallGlobalExceptionLogging()
    {
        DispatcherUnhandledException += (_, args) =>
        {
            Logger.Log($"UNHANDLED (UI thread): {args.Exception}");
            args.Handled = true;
        };
        AppDomain.CurrentDomain.UnhandledException += (_, args) =>
        {
            Logger.Log($"UNHANDLED (non-UI thread, terminating={args.IsTerminating}): {args.ExceptionObject}");
        };
        TaskScheduler.UnobservedTaskException += (_, args) =>
        {
            Logger.Log($"UNOBSERVED TASK EXCEPTION: {args.Exception}");
            args.SetObserved();
        };
    }

    private static string? _shutdownReason;

    /// <summary>
    /// The only way Caroline should shut itself down: logs who asked and why,
    /// with the caller's stack, before calling Shutdown(). Confirmed live
    /// (2026-10-29 23:55): the app closed mid-work with nothing in
    /// caroline.log but "exiting (code 0)" -- the tray Exit path logged
    /// nothing, and Shutdown() is posted to the dispatcher, so OnExit itself
    /// can't see who called it.
    /// </summary>
    public static void RequestShutdown(string reason, bool announce = true)
    {
        _shutdownReason ??= reason;
        Logger.Log($"[App] shutdown requested: {reason}\n{Environment.StackTrace}");
        // An exit somebody asked for: tell the watchdog, or it would restart us.
        if (announce) WatchdogClient.SignalCleanShutdown();
        Current.Dispatcher.Invoke(() => Current.Shutdown());
    }

    protected override void OnSessionEnding(SessionEndingCancelEventArgs e)
    {
        _shutdownReason ??= $"Windows session ending ({e.ReasonSessionEnding})";
        Logger.Log($"[App] {_shutdownReason}");
        WatchdogClient.SignalCleanShutdown();
        base.OnSessionEnding(e);
    }

    protected override void OnExit(ExitEventArgs e)
    {
        Logger.Log($"=== Caroline exiting (code {e.ApplicationExitCode}, reason: {_shutdownReason ?? "UNKNOWN -- Shutdown() called directly, not via App.RequestShutdown"}) ===");
        if (_isFirstInstance)
        {
            _mainWindow?.Cleanup();
            // An orderly exit with no known reason was not announced to the watchdog, so it will
            // treat it as unclean and restart us; leave the run marker so the exit is not lost.
            if (_shutdownReason != null) CrashJournal.MarkStopped();
            _singleInstanceMutex?.ReleaseMutex();
        }
        _singleInstanceMutex?.Dispose();
        base.OnExit(e);
    }

    /// <summary>The watchdog role (--watchdog): runs until the shell exits cleanly or the
    /// watchdog gives up, then ends the process. Blocking waits run off the UI thread.</summary>
    private async Task RunWatchdogModeAsync()
    {
        int code = 1;
        try
        {
            var exe = Environment.ProcessPath!;
            code = await Task.Run(() => WatchdogRunner.RunAsync(new WatchdogOptions
            {
                MainImagePath = exe,
                LaunchMain = restarted =>
                {
                    try
                    {
                        return Process.Start(new ProcessStartInfo(exe)
                        {
                            Arguments = restarted ? "--restarted" : "",
                            UseShellExecute = false,
                            WorkingDirectory = System.IO.Path.GetDirectoryName(exe)!,
                        });
                    }
                    catch (Exception ex)
                    {
                        Logger.Log($"[watchdog] could not start the shell: {ex.Message}");
                        return null;
                    }
                },
                OnGiveUp = msg => Dispatcher.Invoke(() =>
                    System.Windows.MessageBox.Show(msg, "Caroline", MessageBoxButton.OK, MessageBoxImage.Warning)),
            }));
        }
        catch (Exception ex) { Logger.Log($"[watchdog] failed: {ex}"); }
        Logger.Log($"[watchdog] exiting (code {code})");
        Shutdown(code);
    }
}

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

        Logger.Log("=== Caroline starting ===");

        _singleInstanceMutex = new Mutex(true, SingleInstanceMutexName, out _isFirstInstance);
        if (!_isFirstInstance)
        {
            System.Windows.MessageBox.Show("Caroline is already running.", "Caroline",
                MessageBoxButton.OK, MessageBoxImage.Information);
            Shutdown();
            return;
        }

        _settingsService = new SettingsService();
        _settings = _settingsService.Load();

        _mainWindow = new MainWindow(_settings, _settingsService);
        _mainWindow.ExitRequested += (_, _) => Shutdown();
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
        // neither the hotkey nor the backend ever started. Opacity=0 (not
        // Visibility=Hidden) keeps that HWND/Loaded pipeline running exactly
        // as before while keeping the window itself invisible to the user
        // during this window -- see the Opacity=1 restore below for why.
        _mainWindow.Show();
        _mainWindow.Opacity = 0;
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
        // holds once Opacity is restored to 1 further down.
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

        await WaitForSplashDismissAsync(dismissedEarly.Task);
        if (splash.IsLoaded) splash.Close();
        _mainWindow.Opacity = 1;

        _mainWindow.Activate();
        // Activate() alone can silently no-op here: Windows' foreground-lock
        // rules can block a just-launched process from stealing focus from
        // whatever else is in front (confirmed: launched right after
        // CarolineInstaller's own window closes itself, Caroline's window
        // came up not focused/behind other windows despite Show()
        // succeeding). SetForegroundWindow is more forceful and reliably wins.
        NativeMethods.SetForegroundWindow(new WindowInteropHelper(_mainWindow).Handle);

        _updateChecker.Start();

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
    /// Keeps the splash up (and, per OnStartup's Opacity=0/1 dance, the main
    /// window invisible) until the backend actually answers GET /api/status
    /// (started by MainWindow.OnLoaded, already running by the time this is
    /// called) AND no tab in that response's own tabs[] array reports
    /// forcedCompactionPending -- see ChatSession.status()'s own doc comment
    /// (backend-py/app/chat_session.py) for the startup forced-compaction
    /// this is specifically waiting out. Per explicit instruction
    /// (2026-09-13): confirmed live that dismissing on /api/status alone
    /// let the user see a broken-looking, half-populated tab strip while a
    /// large tab's session was still being compacted in the background.
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
        var loggedCompactionWait = false;

        while (DateTime.UtcNow < deadline)
        {
            if (dismissedEarly.IsCompleted) return;

            bool healthy;
            try
            {
                using var resp = await http.GetAsync($"http://127.0.0.1:{BackendProcess.Port}/api/status");
                if (!resp.IsSuccessStatusCode)
                {
                    healthy = false;
                }
                else
                {
                    var body = await resp.Content.ReadAsStringAsync();
                    using var doc = JsonDocument.Parse(body);
                    var compacting = false;
                    if (doc.RootElement.TryGetProperty("tabs", out var tabs) && tabs.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var t in tabs.EnumerateArray())
                        {
                            if (t.TryGetProperty("forcedCompactionPending", out var p) && p.ValueKind == JsonValueKind.True)
                            {
                                compacting = true;
                                break;
                            }
                        }
                    }
                    if (compacting && !loggedCompactionWait)
                    {
                        loggedCompactionWait = true;
                        Logger.Log("App.WaitForSplashDismissAsync: holding splash -- a tab is still running its startup forced compaction");
                    }
                    healthy = !compacting;
                }
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

    protected override void OnExit(ExitEventArgs e)
    {
        Logger.Log($"=== Caroline exiting (code {e.ApplicationExitCode}) ===");
        if (_isFirstInstance)
        {
            _mainWindow?.Cleanup();
            _singleInstanceMutex?.ReleaseMutex();
        }
        _singleInstanceMutex?.Dispose();
        base.OnExit(e);
    }
}

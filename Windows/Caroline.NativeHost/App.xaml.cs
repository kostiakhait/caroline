using System.Threading;
using System.Windows;
using System.Windows.Threading;
using Caroline.NativeHost.Services;

namespace Caroline.NativeHost;

/// <summary>
/// Headless host process for Caroline's embedded multi-window browser (see
/// AppBrowserWindow/AppBrowserHost) -- extracted out of Caroline.exe itself
/// (2026-10-03) so a stuck WebView2 profile or runaway Chromium renderer
/// can't threaten Caroline's own UI process. Provides exactly what
/// AppBrowserWindow needs and nothing else: a WPF Application instance with
/// its own STA message pump (WebView2 requires one) and no MainWindow --
/// every window it ever shows is an on-demand AppBrowserWindow, opened via
/// AppBrowserHost's HTTP bridge.
///
/// Launched lazily by backend-py's app_browser_plugin.py the first time any
/// app_browser_* tool is used in a session (if port 8767 isn't already
/// answering) -- never by Caroline.exe directly, matching the user's own
/// framing of this task ("убрав его из фронтэнда"): the frontend no longer
/// owns this process's lifecycle at all, only the agentic backend does.
/// Also stopped by CarolineInstaller's Autostart.cs (via /shutdown, then a
/// path-filtered hard-kill backstop) before an update, same as Caroline.exe
/// itself.
/// </summary>
public partial class App : System.Windows.Application
{
    // Separate mutex from Caroline.exe's own "Caroline.SingleInstance.Mutex"
    // -- these are two different processes that can legitimately both be
    // running at once; this only guards against backend-py's own lazy-launch
    // racing itself (e.g. two tabs both calling open_app_browser at nearly
    // the same moment and both seeing port 8767 not yet answering).
    private const string SingleInstanceMutexName = "Caroline.NativeHost.SingleInstance.Mutex";
    private Mutex? _singleInstanceMutex;
    private readonly AppBrowserHost _appBrowserHost = new();

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        ShutdownMode = ShutdownMode.OnExplicitShutdown;

        InstallGlobalExceptionLogging();
        Logger.Log("=== Caroline.NativeHost starting ===");

        _singleInstanceMutex = new Mutex(true, SingleInstanceMutexName, out var isFirstInstance);
        if (!isFirstInstance)
        {
            Logger.Log("Caroline.NativeHost: another instance is already running -- exiting quietly");
            Shutdown();
            return;
        }

        try
        {
            _appBrowserHost.Start();
        }
        catch (Exception ex)
        {
            Logger.Log($"Caroline.NativeHost: AppBrowserHost.Start() threw: {ex}");
            Shutdown();
        }
    }

    /// <summary>Same posture as Caroline.exe's own App.xaml.cs: a UI-thread
    /// exception is logged and swallowed (this process staying up in a
    /// possibly-degraded state beats silently vanishing out from under
    /// whichever browser windows it's currently hosting); a non-UI-thread
    /// exception can't be saved this way, logging is purely so there's a
    /// trace afterward.</summary>
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
        Logger.Log($"=== Caroline.NativeHost exiting (code {e.ApplicationExitCode}) ===");
        _appBrowserHost.Dispose();
        try { _singleInstanceMutex?.ReleaseMutex(); } catch { /* never acquired, or already released */ }
        _singleInstanceMutex?.Dispose();
        base.OnExit(e);
    }
}

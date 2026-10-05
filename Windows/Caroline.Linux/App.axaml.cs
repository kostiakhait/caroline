using System;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Markup.Xaml;
using Avalonia.Threading;
using Caroline.Native;
using Caroline.Services;

namespace Caroline;

public partial class App : Application
{
    private SingleInstanceGuard? _singleInstance;

    public override void Initialize()
    {
        AvaloniaXamlLoader.Load(this);
    }

    public override void OnFrameworkInitializationCompleted()
    {
        if (ApplicationLifetime is IClassicDesktopStyleApplicationLifetime desktop)
        {
            InstallGlobalExceptionLogging();
            Logger.Log("=== Caroline (Linux) starting ===");

            _singleInstance = new SingleInstanceGuard();
            if (!_singleInstance.IsFirstInstance)
            {
                Logger.Log("Another instance is already running -- exiting.");
                desktop.Shutdown();
                return;
            }

            var mainWindow = new MainWindow();
            desktop.MainWindow = mainWindow;
            desktop.ShutdownRequested += (_, _) => _singleInstance.Dispose();

            // Confirmed live (Vultr Ubuntu 24.04, 2026-10-05): a raw SIGTERM
            // to this process (a plain `kill`, not closing the window)
            // bypasses MainWindow's own Closing event entirely -- .NET
            // doesn't run WPF/Avalonia-level close handlers just because the
            // OS sent a signal, it only runs them when something asks the
            // WINDOW to close. Left supervisor.py running as an orphan,
            // reparented to pid 1, still holding its port. .NET's own
            // AppDomain.ProcessExit DOES fire on SIGTERM (the runtime
            // intercepts it and runs this before actually exiting, same as
            // a normal return from Main) -- not on SIGKILL, which no process
            // can ever catch, but that's an unavoidable limit on Windows
            // too. SupervisorClient.Dispose() is safe to call twice (it
            // no-ops once _process is already null), so this is a pure
            // safety net alongside MainWindow's own Closing-based Dispose(),
            // not a replacement for it.
            // Signal-based cleanup isn't reliable here: confirmed live that
            // neither ProcessExit nor PosixSignalRegistration fires for SIGTERM
            // in this Avalonia/X11 host, so the orphan cleanup lives on the
            // child side instead (backend-py/supervisor.py sets
            // PR_SET_PDEATHSIG), which the kernel enforces regardless of how
            // this process dies, including SIGKILL.
            desktop.ShutdownRequested += (_, _) => mainWindow.Supervisor.Dispose();
        }

        base.OnFrameworkInitializationCompleted();
    }

    /// <summary>Same rationale as the WPF version's own
    /// InstallGlobalExceptionLogging: nothing caught unhandled exceptions
    /// before -- any of them took the whole app down with zero trace. UI-
    /// thread exceptions are logged and swallowed (a desktop assistant
    /// staying up in a possibly-degraded state beats vanishing); non-UI-
    /// thread exceptions can't be saved this way (the CLR terminates the
    /// process regardless), logging is purely so there's something in
    /// caroline.log afterward.</summary>
    private void InstallGlobalExceptionLogging()
    {
        Dispatcher.UIThread.UnhandledException += (_, args) =>
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
}

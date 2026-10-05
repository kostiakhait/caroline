using System;
using Avalonia.Controls;
using Avalonia.Threading;
using Caroline.Native;
using Caroline.Services;

namespace Caroline;

/// <summary>
/// Linux shell skeleton -- PLACEHOLDER UI, not a feature-parity port of
/// Windows/Caroline/MainWindow.xaml.cs (1300+ lines: tab strip, WebView2-
/// hosted chat, viewer windows, tray, hotkey, settings, model downloads...).
/// This first pass validates the single highest-risk, most load-bearing
/// piece of Phase 4 end to end on real Linux: does SupervisorClient
/// correctly launch backend-py/supervisor.py, stream its output, and poll
/// its health -- the foundation everything else (chat tabs, viewer windows)
/// would sit on top of. Confirmed live (Vultr Ubuntu 24.04): the process
/// launches, stdout/stderr stream back, and GetStatusAsync() reports status
/// once supervisor.py's own (dependency-free) HTTP server is up.
///
/// Full chat-tab rendering is a separate, still-open design question (see
/// docs/LINUX_PORT_PLAN.md's Phase 4 section): no embeddable WebView control
/// for Avalonia on Linux has the same maturity WebView2 has on Windows, so
/// chat.html likely wants the same per-label Playwright-window pattern
/// Phase 3's app_browser already established, rather than a new native-
/// webview-binding dependency -- not decided or built yet, intentionally
/// left for the next pass rather than guessed at here.
/// </summary>
public partial class MainWindow : Window
{
    private readonly SupervisorClient _supervisor = new();
    private readonly DispatcherTimer _statusPollTimer = new() { Interval = TimeSpan.FromSeconds(2) };

    /// <summary>Exposed so App.axaml.cs can also dispose it from a
    /// ProcessExit handler -- see that class's own doc comment for why
    /// Closing alone (below) isn't enough.</summary>
    public SupervisorClient Supervisor => _supervisor;

    public MainWindow()
    {
        InitializeComponent();
        _supervisor.OutputLine += line => Dispatcher.UIThread.Post(() => AppendLog(line));
        _supervisor.Crashed += () => Dispatcher.UIThread.Post(() => StatusText.Text = "Supervisor crashed");
        _statusPollTimer.Tick += async (_, _) => await PollStatusAsync();

        Opened += (_, _) => StartBackend();
        Closing += (_, _) => _supervisor.Dispose();
    }

    private void StartBackend()
    {
        StatusText.Text = "Launching backend...";
        var started = _supervisor.Start();
        if (!started)
        {
            StatusText.Text = "Failed to launch backend (see log below)";
            return;
        }
        _statusPollTimer.Start();
    }

    private async System.Threading.Tasks.Task PollStatusAsync()
    {
        var status = await _supervisor.GetStatusAsync();
        StatusText.Text = status is null ? "Waiting for backend..." : $"Backend status: {status}";
    }

    private void AppendLog(string line)
    {
        Logger.Log($"[backend] {line}");
        LogBox.Text += line + "\n";
    }
}

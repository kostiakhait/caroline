using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;
using Caroline.Interop;
using Caroline.Models;
using Caroline.Native;
using Caroline.Services;
using Caroline.Tray;
using WpfButton = System.Windows.Controls.Button;

namespace Caroline;

public partial class MainWindow : Window
{
    private readonly AppSettings _settings;
    private readonly SettingsService _settingsService;
    private readonly BackendProcess _backend = new();
    private readonly TrayIconManager _tray;
    private GlobalHotkeyService? _hotkey;
    private bool _exitRequested;
    // Tracks Caroline's own open viewer/editor windows by file path so a
    // later close_viewer tool call (see viewer.ts) can find and close the
    // right one -- she can open one, so she needs to be able to close one
    // too, not just leave it to the user.
    private readonly Dictionary<string, DocumentViewerWindow> _viewerWindows = new(StringComparer.OrdinalIgnoreCase);
    private readonly AppBrowserHost _appBrowserHost = new();
    private readonly VisualModeManager _visualMode = new();

    public event EventHandler? ExitRequested;

    public MainWindow(AppSettings settings, SettingsService settingsService)
    {
        InitializeComponent();
        _settings = settings;
        _settingsService = settingsService;
        // For the /test_visual_mode debug endpoint (AppBrowserHost.cs) -- lets a plain
        // curl trigger VisualModeWindow's init in total isolation from the chat/TTS
        // pipeline, so a hang there can be diagnosed without any of that noise.
        _appBrowserHost.VisualMode = _visualMode;

        if (double.IsNaN(_settings.WindowWidth))
        {
            // Never customized/saved yet (fresh install, or a settings
            // reset) -- default to pinned against the right edge of the
            // primary screen's work area (SystemParameters.WorkArea already
            // excludes the taskbar, on whichever edge it's docked), 20% of
            // screen width, full available height. The user can freely
            // move/resize from here -- see the save point further down,
            // which persists whatever they end up with and makes this
            // branch never run again for them.
            var workArea = SystemParameters.WorkArea;
            Width = workArea.Width * 0.2;
            Height = workArea.Height;
            Left = workArea.Right - Width;
            Top = workArea.Top;
        }
        else
        {
            if (!double.IsNaN(_settings.WindowLeft)) Left = _settings.WindowLeft;
            if (!double.IsNaN(_settings.WindowTop)) Top = _settings.WindowTop;
            Width = _settings.WindowWidth;
            Height = _settings.WindowHeight;
        }
        Topmost = _settings.AlwaysOnTop;

        _tray = new TrayIconManager();
        _tray.OpenRequested += (_, _) => Dispatcher.Invoke(ShowAndActivate);
        _tray.ExitRequested += (_, _) => Dispatcher.Invoke(RequestExit);
        _tray.UpdateRequested += (_, _) => UpdateRequested?.Invoke(this, EventArgs.Empty);
    }

    /// <summary>Bubbles up the tray's "Update to ..." click -- App.xaml.cs owns the
    /// UpdateChecker instance that actually knows how to perform it.</summary>
    public event EventHandler? UpdateRequested;

    public void ShowUpdateAvailable(string version) => _tray.ShowUpdateAvailable(version);
    public void HideUpdateAvailable() => _tray.HideUpdateAvailable();

    private bool _updatePopupShownThisDownload;

    /// <summary>Per explicit instruction (2026-09-06): a download that used to run silently for
    /// 2+ minutes (confirmed live) now shows up in two places -- a one-time tray balloon the
    /// moment it starts, and this same status text pushed into EVERY open tab's status bar for
    /// the whole download (see UpdateChecker.StatusChanged and chat.js's "update_status"
    /// handler). _updatePopupShownThisDownload resets only when App.xaml.cs starts a new
    /// UpdateChecker (i.e. never, within one process lifetime) -- one popup per download is the
    /// point, not one per percent tick.</summary>
    public void BroadcastUpdateStatus(string text)
    {
        if (!_updatePopupShownThisDownload)
        {
            _updatePopupShownThisDownload = true;
            _tray.ShowUpdateOngoingPopup();
        }
        var payload = JsonSerializer.Serialize(new { type = "update_status", text });
        foreach (var tab in _tabs)
        {
            try { tab.WebView?.CoreWebView2?.PostWebMessageAsJson(payload); }
            catch (Exception ex) { Logger.Log($"MainWindow: BroadcastUpdateStatus failed for tab {tab.Id}: {ex.Message}"); }
        }
    }

    // Index 0: show/hide the window (user-configurable, see AppSettings).
    // Index 1: fixed Ctrl+Shift+C, starts/stops voice recording without
    // needing the window open first -- works even while hidden to tray
    // since the WebView2/backend keep running underneath (Hide() doesn't
    // tear anything down, see OnClosing).
    private const int HotkeyIndexToggleVisibility = 0;
    private const int HotkeyIndexVoiceRecord = 1;

    protected override void OnSourceInitialized(EventArgs e)
    {
        base.OnSourceInitialized(e);
        _hotkey = new GlobalHotkeyService(this);
        if (!_hotkey.Register(HotkeyIndexToggleVisibility, _settings.HotkeyModifiers, _settings.HotkeyVirtualKey))
        {
            Logger.Log("[hotkey] registration failed -- already in use by another app?");
        }
        if (!_hotkey.Register(HotkeyIndexVoiceRecord, NativeModifiers.Control | NativeModifiers.Shift, VirtualKeyC))
        {
            Logger.Log("[hotkey] voice-record hotkey registration failed -- already in use by another app?");
        }
        _hotkey.HotkeyPressed += (index) => Dispatcher.Invoke(() =>
        {
            if (index == HotkeyIndexToggleVisibility) ToggleVisibility();
            else if (index == HotkeyIndexVoiceRecord) ToggleVoiceRecordingFromHotkey();
        });
    }

    private const uint VirtualKeyC = 0x43;

    /// <summary>
    /// Triggers the same mic toggle the chat page's own button does, via
    /// ExecuteScriptAsync -- targets whichever tab is currently active/
    /// visible (there's only one meaningful target for a global hotkey with
    /// several tabs open); works whether or not the window is currently
    /// visible, since WebView2 keeps running while merely Hidden.
    /// </summary>
    private void ToggleVoiceRecordingFromHotkey()
    {
        Logger.Log("MainWindow: ToggleVoiceRecordingFromHotkey (Ctrl+Shift+C)");
        _ = _activeTab?.WebView?.CoreWebView2?.ExecuteScriptAsync("window.carolineToggleVoiceRecording && window.carolineToggleVoiceRecording();");
    }

    // Backend-crash restart policy -- same shape as the backend's own
    // internal MAX_RESTARTS_PER_WINDOW guard against a session that keeps
    // dying (server.ts's handleFailure): a handful of quick auto-restarts
    // are fine (transient), but a backend that keeps crashing needs a human,
    // not an infinite respawn loop silently burning CPU/battery.
    private readonly List<DateTime> _backendRestartTimestamps = new();
    private const int MaxBackendRestartsPerWindow = 5;
    private static readonly TimeSpan BackendRestartWindow = TimeSpan.FromMinutes(10);
    private BackendHealthWatchdog? _healthWatchdog;
    // One per open tab, created/disposed alongside AddTabAsync/CloseTabAsync
    // -- see BackendHealthWatchdog's own doc comment for why this replaced
    // the single shared instance's old "check primary, kill everything"
    // design (2026-09-08).
    private readonly Dictionary<string, BackendHealthWatchdog> _tabWatchdogs = new();
    private readonly HttpClient _statusHttp = new() { Timeout = TimeSpan.FromSeconds(10) };

    private int _onLoadedCallCount;

    private async void OnLoaded(object sender, RoutedEventArgs e)
    {
        _onLoadedCallCount++;
        Logger.Log($"MainWindow.OnLoaded: starting backend (call #{_onLoadedCallCount} -- WPF's Loaded event " +
            "firing more than once would explain a stray/duplicate health watchdog; logging this to confirm or rule it out)");
        _backend.OutputLine += line => Logger.Log($"[backend] {line}");
        // Bug fix (2026-09-11), per a real live incident: this used to be a BLOCKING
        // Dispatcher.Invoke. Crashed fires from Process.Exited's own callback machinery, and
        // synchronously waiting for the UI thread to run RestartBackend -> BackendProcess.Dispose()
        // -> Process.Dispose() on that SAME Process object, while its Exited callback is still
        // "in flight", deadlocks on Process's internal wait-handle unregistration -- confirmed
        // live: froze the entire window for 40+ minutes. BeginInvoke lets Process.Exited's own
        // callback return immediately; RestartBackend then runs later, genuinely outside that
        // callback's call stack, so this specific reentrancy can't happen anymore. (See also
        // BackendProcess.Dispose()'s own 5s timeout bound, added as a backstop alongside this.)
        _backend.Crashed += () => Dispatcher.BeginInvoke(() =>
        {
            try { RestartBackend("crashed"); }
            catch (Exception ex) { Logger.Log($"MainWindow: RestartBackend threw while handling Crashed: {ex}"); }
        });
        if (!_backend.Start())
        {
            ShowError("Backend failed to start -- is Node.js installed?");
            return;
        }

        try { _appBrowserHost.Start(); }
        catch (Exception ex) { Logger.Log($"MainWindow: AppBrowserHost.Start() threw: {ex}"); }

        // Independent of anything inside the backend's own event loop -- see
        // BackendHealthWatchdog's doc comment for why that matters (it can
        // catch a full event-loop freeze that no internal timer ever could).
        // Process.Kill(entireProcessTree:true) inside RestartBackend works
        // fine even on a fully-frozen process (it's a kernel-level
        // TerminateProcess, doesn't need the target to cooperate).
        _healthWatchdog = new BackendHealthWatchdog(BackendProcess.Port);
        _healthWatchdog.LogLine += line => Logger.Log(line);
        _healthWatchdog.Frozen += (reason) =>
        {
            // Logged BEFORE Dispatcher.Invoke, on the watchdog's own
            // thread-pool thread -- so if the invoke itself never runs (UI
            // thread stuck, exception swallowed somewhere, whatever), the
            // log still shows the event actually fired and was handed off,
            // narrowing "did Frozen fire?" vs "did the UI-thread handler run?"
            // instead of having to guess between them again.
            Logger.Log($"MainWindow: Frozen event received (reason: {reason}) -- dispatching to UI thread");
            try
            {
                // BeginInvoke (2026-09-11), not Invoke -- see the Crashed handler's own comment
                // above for the deadlock this class of bug can cause. Frozen fires from
                // BackendHealthWatchdog's own thread rather than Process.Exited, so the specific
                // reentrancy there doesn't apply here, but there's no reason for this thread to
                // block on RestartBackend either, and consistency matters more than a provably
                // narrower fix.
                Dispatcher.BeginInvoke(() =>
                {
                    Logger.Log($"MainWindow: external health check says the backend is unresponsive: {reason}");
                    try
                    {
                        // No message box for this, deliberately -- explicit
                        // user request (2026-08-31, reaffirmed 2026-09-05):
                        // routine auto-restarts must be silent (they were
                        // confirmed live to stack into multiple blocking
                        // dialogs when the backend needed several restarts in
                        // a row), reasons belong in caroline.log, not a
                        // popup. Even repeated failures never escalate to a
                        // blocking dialog anymore -- see server.ts's
                        // handleFailure, which backs off and retries forever
                        // instead of giving up.
                        RestartBackend("stopped responding (detected externally)");
                    }
                    catch (Exception ex)
                    {
                        Logger.Log($"MainWindow: RestartBackend threw while handling Frozen: {ex}");
                    }
                });
            }
            catch (Exception ex)
            {
                Logger.Log($"MainWindow: Dispatcher.Invoke for Frozen threw: {ex}");
            }
        };

        await InitTabsAsync();
    }

    /// <summary>
    /// Recovers ONE stuck tab without touching the shared backend process or
    /// any other tab -- the tab-scoped counterpart to RestartBackend (see its
    /// own doc comment for why that one still exists, whole-process-only,
    /// for genuine unreachability). Fetches this tab's own cliProcessPid from
    /// /api/status (added 2026-09-08 specifically for this) and kills just
    /// that OS process tree directly, in-process (same TerminateProcess
    /// primitive AppBrowserHost's /kill_process already exposes to the Node
    /// side -- no need to go through that HTTP bridge here, this IS that
    /// same .NET process). Killing it ends that tab's own query() stream,
    /// which the backend's OWN internal handleFailure already notices and
    /// recovers from on its own (fresh query(), resumed session, the
    /// existing watchdogNote telling Caroline what happened) -- no separate
    /// "tell the backend to restart" call needed.
    /// </summary>
    private async void RecoverTab(string tabId, string reason)
    {
        Logger.Log($"MainWindow.RecoverTab({tabId}): entered (reason={reason})");
        try
        {
            using var resp = await _statusHttp.GetAsync($"http://127.0.0.1:{BackendProcess.Port}/api/status");
            if (!resp.IsSuccessStatusCode)
            {
                Logger.Log($"MainWindow.RecoverTab({tabId}): /api/status returned {(int)resp.StatusCode} -- can't identify this tab's pid, giving up (the whole-process watchdog will catch it if the backend itself is actually down)");
                return;
            }
            var body = await resp.Content.ReadAsStringAsync();
            using var doc = JsonDocument.Parse(body);
            if (!doc.RootElement.TryGetProperty("tabs", out var tabs) || tabs.ValueKind != JsonValueKind.Array)
            {
                Logger.Log($"MainWindow.RecoverTab({tabId}): /api/status response has no tabs[] array -- giving up");
                return;
            }
            int? pid = null;
            foreach (var t in tabs.EnumerateArray())
            {
                if (t.TryGetProperty("tabId", out var id) && id.GetString() == tabId)
                {
                    if (t.TryGetProperty("cliProcessPid", out var p) && p.ValueKind == JsonValueKind.Number) pid = p.GetInt32();
                    break;
                }
            }
            if (pid == null)
            {
                Logger.Log($"MainWindow.RecoverTab({tabId}): no cliProcessPid known for this tab yet (query() may still be starting) -- nothing to kill, backend's own internal watchdog is still the primary recovery path here");
                return;
            }
            try
            {
                using var proc = Process.GetProcessById(pid.Value);
                proc.Kill(entireProcessTree: true);
                Logger.Log($"MainWindow.RecoverTab({tabId}): killed pid={pid} and its tree -- backend's own handleFailure will resume this tab's session with a fresh query()");
            }
            catch (ArgumentException)
            {
                Logger.Log($"MainWindow.RecoverTab({tabId}): pid={pid} already gone -- nothing to do");
            }
            // Same reasoning as RestartBackend's own NotifyBackendRestarted
            // call: without this, this tab's own watchdog could declare it
            // frozen again 60-90s later, while the fresh query() is still
            // legitimately starting up, before it's even had a chance.
            if (_tabWatchdogs.TryGetValue(tabId, out var tabWatchdog)) tabWatchdog.NotifyBackendRestarted();
        }
        catch (Exception ex)
        {
            Logger.Log($"MainWindow.RecoverTab({tabId}): failed: {ex}");
        }
    }

    /// <summary>
    /// Shared by both recovery paths: BackendProcess.Crashed (the process
    /// exited on its own) and BackendHealthWatchdog.Frozen (the process is
    /// alive but not answering at all -- see that class's own doc comment
    /// for why that's the one case left genuinely whole-process). Same
    /// MaxBackendRestartsPerWindow give-up threshold either way: a backend
    /// that keeps needing rescue, whether by crashing or by being completely
    /// unreachable, needs a human, not an infinite respawn loop. One backend
    /// process serves every tab (see the `sessions` map on the Node side),
    /// so THIS restart is shared across all of them, not per-tab -- a single
    /// stuck tab is RecoverTab's job instead (see its own doc comment), not
    /// this one's.
    /// </summary>
    private void RestartBackend(string reasonForLog, string? extraUserNote = null)
    {
        Logger.Log($"MainWindow.RestartBackend: entered (reason={reasonForLog}, thread={Environment.CurrentManagedThreadId}, isUIThread={Dispatcher.CheckAccess()})");
        var now = DateTime.UtcNow;
        _backendRestartTimestamps.RemoveAll(t => now - t > BackendRestartWindow);
        _backendRestartTimestamps.Add(now);
        if (_backendRestartTimestamps.Count > MaxBackendRestartsPerWindow)
        {
            Logger.Log($"MainWindow: backend needed rescue ({reasonForLog}) {_backendRestartTimestamps.Count} times in {BackendRestartWindow.TotalMinutes} min -- giving up auto-restart.");
            ShowError("Caroline's backend keeps failing. Check caroline.log, or restart the app.\n\n" + Logger.LogPath);
            return;
        }

        Logger.Log($"MainWindow: restarting backend ({reasonForLog}) (attempt {_backendRestartTimestamps.Count}/{MaxBackendRestartsPerWindow})");
        // Kill(entireProcessTree:true) -- confirmed necessary, not just
        // belt-and-suspenders: a plain Kill() on just this direct child left
        // its own grandchildren (claude.exe and, under it, every MCP server
        // subprocess) running as orphans across restarts, confirmed live as
        // dozens of accumulated stray node.exe processes over a single day
        // of intermittent crashes/restarts (2026-08-30).
        Logger.Log("MainWindow.RestartBackend: disposing old backend process...");
        var disposeStart = DateTime.UtcNow;
        _backend.Dispose();
        Logger.Log($"MainWindow.RestartBackend: old backend disposed in {(DateTime.UtcNow - disposeStart).TotalSeconds:F1}s, starting new one...");
        var startStart = DateTime.UtcNow;
        bool started;
        try
        {
            started = _backend.Start();
        }
        catch (Exception ex)
        {
            Logger.Log($"MainWindow.RestartBackend: _backend.Start() threw after {(DateTime.UtcNow - startStart).TotalSeconds:F1}s: {ex}");
            ShowError($"Backend could not be restarted: {ex.Message}");
            return;
        }
        Logger.Log($"MainWindow.RestartBackend: _backend.Start() returned {started} after {(DateTime.UtcNow - startStart).TotalSeconds:F1}s");
        if (started)
        {
            // Root-caused live on 2026-08-31: without this, the health
            // watchdog kept declaring THIS fresh process "frozen" (it just
            // needs normal startup time, especially under heavy system
            // load) and killing it before it ever finished coming up --
            // an exact-clockwork restart every ~60s, forever. See
            // BackendHealthWatchdog.NotifyBackendRestarted's own comment.
            _healthWatchdog?.NotifyBackendRestarted();
            // A whole-process restart means every open tab's own query() is
            // about to cold-start too -- same "don't judge it before it's
            // had a chance" reasoning as the line above, just extended to
            // every per-tab watchdog instead of only the shared one.
            foreach (var tabWatchdog in _tabWatchdogs.Values) tabWatchdog.NotifyBackendRestarted();
        }
        if (!started)
        {
            ShowError("Backend could not be restarted -- is Node.js installed?");
            return;
        }
        if (extraUserNote != null)
        {
            // MessageBox.Show(this, ...) is modal -- it blocks this thread
            // until dismissed. If the window was hidden to tray (Hide(),
            // not Close() -- see OnClosing/ToggleVisibility) when this
            // fires, an owned MessageBox can end up not actually visible to
            // the user while still blocking, which would silently stall
            // every recovery after it forever (confirmed live: exactly one
            // "restarting backend" log line ever appeared across hours of
            // repeated freezes, then nothing -- consistent with this).
            // ShowAndActivate() first guarantees the owner window (and so
            // the dialog) is actually on screen and focused.
            Logger.Log("MainWindow.RestartBackend: showing MessageBox to the user (this call blocks until dismissed) -- forcing window visible first");
            ShowAndActivate();
            System.Windows.MessageBox.Show(this, extraUserNote, "Caroline restarted", System.Windows.MessageBoxButton.OK, System.Windows.MessageBoxImage.Warning);
            Logger.Log("MainWindow.RestartBackend: MessageBox dismissed.");
        }
        // Every open tab's own chat.js WS reconnect loop (ws.onclose -> retry
        // every 1.5s) picks the new backend up on its own once it's listening
        // again -- no need to reload any WebView2 page for this case.
        Logger.Log("MainWindow.RestartBackend: done.");
    }

    // --- Multi-tab chat (up to MaxTabs concurrent, independent conversations) ---
    // One backend process, one Node-side `sessions` Map keyed by tabId (see
    // server.ts) -- each tab here is just a WebView2 instance pointed at
    // chat.html?tab=<id>, all sharing ONE CoreWebView2Environment/profile
    // (chat.html holds no per-tab-sensitive browser state of its own, unlike
    // AppBrowserWindow's per-label profiles) so opening a tab doesn't pay a
    // fresh WebView2-runtime cold start, only the backend session behind it
    // does (paid once, when that specific tab is actually opened -- not all
    // at once at app startup).
    private sealed class ChatTab
    {
        public required string Id;
        public required Grid ContentHost;
        public required WpfButton HeaderButton;
        public required WpfButton CloseButton;
        public required string Name;
        // Built once (in AddTabAsync) and reused by every RebuildTabStrip()
        // call -- confirmed live (2026-08-31) that creating a NEW DockPanel
        // wrapper each time and re-adding the SAME HeaderButton/CloseButton
        // instances into it throws InvalidOperationException ("already the
        // logical child of another element"), since those buttons are still
        // logical children of the PREVIOUS call's now-orphaned wrapper.
        // RebuildTabStrip only re-inserts this same wrapper into TabStrip
        // and restyles it in place, never creates a new one.
        public required DockPanel HeaderWrapper;
        public WebView2? WebView;
    }

    private const int MaxTabs = 5;
    private readonly List<ChatTab> _tabs = new();
    private ChatTab? _activeTab;
    private WpfButton? _addTabButton;
    private CoreWebView2Environment? _sharedWebViewEnv;

    private async Task InitTabsAsync()
    {
        var idsToOpen = (_settings.OpenTabIds is { Count: > 0 } ? _settings.OpenTabIds : new List<string> { "1" })
            .Distinct().Take(MaxTabs).ToList();
        foreach (var id in idsToOpen)
        {
            await AddTabAsync(id, selectAfter: false);
        }
        if (_tabs.Count > 0) SelectTab(_tabs[0]);
    }

    // Same reasoning as AppBrowserWindow.EnsureInitializedAsync's own copy of
    // this (see its doc comment -- confirmed live 2026-09-01 a stuck WebView2
    // init there hung forever with no way out): this is the CHAT UI's own
    // WebView2 environment, so a hang here is even more severe -- the whole
    // app becomes unusable, not just one embedded-browser label. Audited in
    // after finding and fixing that one.
    private static readonly TimeSpan WebViewInitTimeout = TimeSpan.FromSeconds(60);

    private static async Task<T> TimeoutAfter<T>(Task<T> task, TimeSpan timeout, string what)
    {
        var completed = await Task.WhenAny(task, Task.Delay(timeout));
        if (completed != task) throw new TimeoutException($"{what} did not complete within {timeout.TotalSeconds:F0}s.");
        return await task;
    }

    private static async Task TimeoutAfter(Task task, TimeSpan timeout, string what)
    {
        var completed = await Task.WhenAny(task, Task.Delay(timeout));
        if (completed != task) throw new TimeoutException($"{what} did not complete within {timeout.TotalSeconds:F0}s.");
        await task;
    }

    private async Task<ChatWebViewEnvResult> EnsureSharedWebViewEnvAsync()
    {
        if (_sharedWebViewEnv != null) return new ChatWebViewEnvResult(_sharedWebViewEnv, null);
        try
        {
            var dataDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "Caroline", "webview2");
            _sharedWebViewEnv = await TimeoutAfter(
                CoreWebView2Environment.CreateAsync(userDataFolder: dataDir), WebViewInitTimeout, "CoreWebView2Environment.CreateAsync (chat)");
            return new ChatWebViewEnvResult(_sharedWebViewEnv, null);
        }
        catch (Exception ex)
        {
            return new ChatWebViewEnvResult(null, ex);
        }
    }

    private readonly record struct ChatWebViewEnvResult(CoreWebView2Environment? Env, Exception? Error);

    private string GenerateNextTabId()
    {
        for (var i = 1; i <= MaxTabs; i++)
        {
            var id = i.ToString();
            if (_tabs.All(t => t.Id != id)) return id;
        }
        throw new InvalidOperationException("No free tab id -- caller must check _tabs.Count < MaxTabs first.");
    }

    private async Task AddTabAsync(string tabId, bool selectAfter)
    {
        if (_tabs.Count >= MaxTabs)
        {
            Logger.Log($"MainWindow.AddTabAsync: refused, already at MaxTabs ({MaxTabs})");
            return;
        }
        Logger.Log($"MainWindow.AddTabAsync: adding tab {tabId}");

        var contentHost = new Grid { Visibility = Visibility.Collapsed };
        TabContentHost.Children.Add(contentHost);

        var tabName = _settings.TabNames.TryGetValue(tabId, out var savedName) && !string.IsNullOrWhiteSpace(savedName)
            ? savedName
            : $"Tab {tabId}";

        var headerButton = new WpfButton
        {
            Content = tabName,
            Padding = new Thickness(10, 6, 10, 6),
            Margin = new Thickness(0),
            Background = System.Windows.Media.Brushes.Transparent,
            Foreground = System.Windows.Media.Brushes.White,
            BorderThickness = new Thickness(0),
        };
        var closeButton = new WpfButton
        {
            Content = "✕",
            Padding = new Thickness(4, 0, 4, 0),
            Margin = new Thickness(0, 0, 4, 0),
            Background = System.Windows.Media.Brushes.Transparent,
            Foreground = System.Windows.Media.Brushes.LightGray,
            BorderThickness = new Thickness(0),
            ToolTip = "Close tab",
        };

        var headerWrapper = new DockPanel { LastChildFill = true };
        DockPanel.SetDock(closeButton, Dock.Right);
        headerWrapper.Children.Add(closeButton);
        headerWrapper.Children.Add(headerButton);

        var tab = new ChatTab
        {
            Id = tabId, ContentHost = contentHost, HeaderButton = headerButton,
            CloseButton = closeButton, HeaderWrapper = headerWrapper, Name = tabName,
        };
        headerButton.Click += (_, _) => SelectTab(tab);
        headerButton.MouseDoubleClick += (_, e) => { BeginRenameTab(tab); e.Handled = true; };
        closeButton.Click += async (_, _) => await CloseTabAsync(tab);

        _tabs.Add(tab);
        RebuildTabStrip();
        PersistOpenTabIds();

        // Per explicit instruction (2026-09-08): a stuck tab must be
        // recoverable on its own, without taking every other tab down --
        // see BackendHealthWatchdog's own doc comment. One instance per
        // open tab, disposed in CloseTabAsync.
        var tabWatchdog = new BackendHealthWatchdog(BackendProcess.Port, tabId);
        tabWatchdog.LogLine += line => Logger.Log(line);
        tabWatchdog.TabFrozen += (frozenTabId, reason) => Dispatcher.Invoke(() => RecoverTab(frozenTabId, reason));
        _tabWatchdogs[tabId] = tabWatchdog;

        await InitWebViewForTabAsync(tab);
        if (selectAfter) SelectTab(tab);
    }

    private async Task InitWebViewForTabAsync(ChatTab tab)
    {
        var webView = new WebView2();
        tab.ContentHost.Children.Add(webView);
        tab.WebView = webView;

        var (env, envError) = await EnsureSharedWebViewEnvAsync();
        if (env == null)
        {
            if (envError != null && envError.GetType().Name.Contains("WebView2RuntimeNot"))
            {
                ShowError("Microsoft Edge WebView2 Runtime is not installed.\nDownload it from: aka.ms/webview2");
            }
            else
            {
                Logger.Log($"InitWebViewForTabAsync({tab.Id}): shared environment creation failed: {envError}");
                ShowError($"Failed to initialize browser:\n{envError?.Message}");
            }
            return;
        }

        try
        {
            await TimeoutAfter(webView.EnsureCoreWebView2Async(env), WebViewInitTimeout, $"EnsureCoreWebView2Async (chat tab {tab.Id})");

            webView.CoreWebView2.NavigationCompleted += (_, args) =>
            {
                if (!args.IsSuccess)
                    ShowError($"Could not load chat UI (error {args.WebErrorStatus}).");
            };

            // Confirmed real failure mode: the WPF window/tray can stay
            // completely alive and responsive while WebView2's own render
            // process crashes or hangs underneath -- the chat page then
            // looks "frozen" (no button does anything) even though nothing
            // in .NET actually died. ProcessFailed is WebView2's own
            // recovery hook for exactly this; without it, this class of
            // failure had no recovery path at all short of the user
            // manually restarting the whole app.
            webView.CoreWebView2.ProcessFailed += (_, args) => Dispatcher.Invoke(() => OnWebViewProcessFailed(tab, args));

            // Without this, WebView2 falls back to its own browser-style
            // permission prompt for getUserMedia -- confirmed live: wrong UX
            // for a desktop app (the user isn't a website asking for mic
            // access, this *is* the app), and the prompt's own bar rendered
            // inside the borderless window was easy to miss/never actually
            // resolve the pending getUserMedia() promise either way. Caroline
            // only ever asks for the microphone (voice input), so grant that
            // silently; everything else keeps the default (Deny) behavior.
            webView.CoreWebView2.PermissionRequested += (_, args) =>
            {
                if (args.PermissionKind == CoreWebView2PermissionKind.Microphone)
                {
                    args.State = CoreWebView2PermissionState.Allow;
                }
            };

            // Round trip for the "open_in_viewer" backend tool: chat.js
            // relays the backend's "open_editor" WS push here via
            // window.chrome.webview.postMessage, we open the floating
            // DocumentViewerWindow, and post the outcome back the same way
            // once it closes -- see viewer.ts for the other end of this.
            // Per-tab: each tab's own webView instance posts back to itself.
            webView.CoreWebView2.WebMessageReceived += (_, args) => OnWebMessageReceived(webView, args);

            // Not file:///... -- confirmed live via DevTools this breaks the
            // persona reference photos (<img src="assets/..."> relative to
            // chat.html): Chromium/WebView2 treats every file: URL as its
            // own unique security origin, so a same-directory relative
            // subresource load gets silently rejected as net::ERR_FILE_NOT_
            // FOUND even though the file genuinely exists on disk. Mapping
            // wwwroot to a virtual https:// host is Microsoft's documented
            // fix for exactly this (local web content with normal relative-
            // resource loading), and gives chat.html a real, stable origin
            // instead of the one-off-per-URL file: pseudo-origin.
            var wwwrootDir = Path.Combine(AppContext.BaseDirectory, "wwwroot");
            webView.CoreWebView2.SetVirtualHostNameToFolderMapping(
                "caroline.local", wwwrootDir, CoreWebView2HostResourceAccessKind.Allow);
            // Bug fix (2026-09-09): the WebView2 profile itself is persistent
            // across app restarts (see EnsureSharedWebViewEnvAsync's own
            // userDataFolder) -- confirmed live that a rebuilt chat.js/chat.css
            // dropped into wwwroot/ was NOT picked up by a plain app restart,
            // because the browser cache from a PREVIOUS run's fetch of
            // https://caroline.local/chat.js was still being served. Clearing
            // the cache for this virtual host's own resources right before
            // every navigation guarantees a fresh fetch every single time,
            // not just the first one after install -- cheap (local disk
            // cache only, not cookies/login state) and this is the only
            // place chat.html/js/css/wwwroot assets are ever loaded from.
            try
            {
                // CoreWebView2Profile.ClearBrowsingDataAsync isn't available in
                // this project's pinned WebView2 SDK version (1.0.2957.106) --
                // the DevTools Protocol's Network.clearBrowserCache has been
                // present since WebView2's earliest versions and does the same
                // thing, reached via the always-available CallDevToolsProtocolMethodAsync.
                await webView.CoreWebView2.CallDevToolsProtocolMethodAsync("Network.clearBrowserCache", "{}");
            }
            catch (Exception ex)
            {
                Logger.Log($"InitWebViewForTabAsync({tab.Id}): clearBrowserCache threw (ignored, navigating anyway): {ex.Message}");
            }
            // ?tab=<id> -- routes this WS connection to the right per-tab
            // ChatSession on the Node side (server.ts's `sessions` map) and
            // keys this tab's own localStorage transcript (chat.js), since
            // all tabs share one WebView2 profile/origin.
            // alwaysOnTop passed as a query param (read once, synchronously, by chat.js at
            // load) rather than pushed via postMessage after the fact -- this is purely a
            // native/WPF setting the backend has no stake in, so there's no round trip to
            // wait on and no race to worry about; the page just knows its initial state
            // the same way it already knows its own port/tab.
            // assetsVersion -- see chat.html's own doc comment: the actual on-disk
            // mtime of chat.js/chat.css, so a changed file always gets a different
            // URL and can never be served stale regardless of any WebView2 caching
            // layer's own behavior (clearBrowserCache above is belt-and-suspenders,
            // not the only thing this now depends on).
            long assetsVersion = 0;
            try
            {
                var chatJsWrite = File.GetLastWriteTimeUtc(Path.Combine(wwwrootDir, "chat.js")).Ticks;
                var chatCssWrite = File.GetLastWriteTimeUtc(Path.Combine(wwwrootDir, "chat.css")).Ticks;
                assetsVersion = Math.Max(chatJsWrite, chatCssWrite);
            }
            catch (Exception ex)
            {
                Logger.Log($"InitWebViewForTabAsync({tab.Id}): failed to read chat.js/chat.css mtime for assetsVersion (falling back to 0, cache-busting won't work this launch): {ex.Message}");
            }
            webView.Source = new Uri($"https://caroline.local/chat.html?port={BackendProcess.Port}&tab={Uri.EscapeDataString(tab.Id)}&alwaysOnTop={(_settings.AlwaysOnTop ? "1" : "0")}&assetsVersion={assetsVersion}");
        }
        catch (Exception ex) when (ex.GetType().Name.Contains("WebView2RuntimeNot"))
        {
            ShowError("Microsoft Edge WebView2 Runtime is not installed.\nDownload it from: aka.ms/webview2");
        }
        catch (Exception ex)
        {
            Logger.Log($"InitWebViewForTabAsync({tab.Id}) failed: {ex}");
            ShowError($"Failed to initialize browser:\n{ex.Message}");
        }
    }

    private async void OnWebViewProcessFailed(ChatTab tab, CoreWebView2ProcessFailedEventArgs args)
    {
        Logger.Log($"WebView2 ProcessFailed (tab {tab.Id}): kind={args.ProcessFailedKind}, exitCode={args.ExitCode}, reason={args.Reason}");

        // A crashed/unresponsive *renderer* for the page we're already
        // showing can just be reloaded in place. A dead *browser* process
        // (the whole WebView2 engine, shared across every tab) takes every
        // tab's WebView2 control down with it -- if this specific tab's
        // reload fails, only THIS tab's control gets recreated; the others
        // will independently hit and handle their own ProcessFailed too.
        if (args.ProcessFailedKind is CoreWebView2ProcessFailedKind.RenderProcessExited
            or CoreWebView2ProcessFailedKind.RenderProcessUnresponsive)
        {
            try
            {
                tab.WebView?.Reload();
                return;
            }
            catch (Exception ex)
            {
                Logger.Log($"WebView2 reload after ProcessFailed failed (tab {tab.Id}), recreating control: {ex}");
            }
        }

        if (tab.WebView != null) tab.ContentHost.Children.Remove(tab.WebView);
        tab.WebView = null;
        await InitWebViewForTabAsync(tab);
    }

    /// <summary>
    /// Rebuilds the tab strip's header row from _tabs -- called on every
    /// add/close/select rather than trying to patch it incrementally, since
    /// with at most MaxTabs (5) entries the whole thing is cheap to redo and
    /// this avoids an entire class of "the strip's visual state drifted from
    /// _tabs" bugs.
    /// </summary>
    private void RebuildTabStrip()
    {
        TabStrip.Children.Clear();
        TabStrip.Visibility = Visibility.Visible;
        foreach (var tab in _tabs)
        {
            var isActive = tab == _activeTab;
            tab.HeaderButton.Background = isActive
                ? System.Windows.Media.Brushes.RoyalBlue
                : System.Windows.Media.Brushes.Transparent;
            // Never allow closing the last tab -- there must always be
            // somewhere to actually chat.
            tab.CloseButton.Visibility = _tabs.Count > 1 ? Visibility.Visible : Visibility.Collapsed;

            // Reuse the SAME wrapper built once in AddTabAsync -- see
            // ChatTab.HeaderWrapper's doc comment for why a fresh DockPanel
            // here (re-parenting the same buttons into it) throws.
            TabStrip.Children.Add(tab.HeaderWrapper);
        }

        _addTabButton = new WpfButton
        {
            Content = "+",
            Padding = new Thickness(10, 6, 10, 6),
            Background = System.Windows.Media.Brushes.Transparent,
            Foreground = System.Windows.Media.Brushes.White,
            BorderThickness = new Thickness(0),
            IsEnabled = _tabs.Count < MaxTabs,
            ToolTip = _tabs.Count < MaxTabs ? "New tab" : $"Maximum {MaxTabs} tabs",
        };
        _addTabButton.Click += async (_, _) =>
        {
            if (_tabs.Count >= MaxTabs) return;
            await AddTabAsync(GenerateNextTabId(), selectAfter: true);
        };
        TabStrip.Children.Add(_addTabButton);
    }

    private void SelectTab(ChatTab tab)
    {
        _activeTab = tab;
        foreach (var t in _tabs) t.ContentHost.Visibility = t == tab ? Visibility.Visible : Visibility.Collapsed;
        RebuildTabStrip();
    }

    private async Task CloseTabAsync(ChatTab tab)
    {
        if (_tabs.Count <= 1) return; // never close the last tab
        Logger.Log($"MainWindow.CloseTabAsync: closing tab {tab.Id}");

        var wasActive = tab == _activeTab;
        var index = _tabs.IndexOf(tab);
        _tabs.Remove(tab);
        TabContentHost.Children.Remove(tab.ContentHost);
        try { tab.WebView?.Dispose(); } catch (Exception ex) { Logger.Log($"CloseTabAsync({tab.Id}): WebView.Dispose() threw: {ex}"); }
        if (_tabWatchdogs.Remove(tab.Id, out var tabWatchdog))
        {
            try { tabWatchdog.Dispose(); } catch (Exception ex) { Logger.Log($"CloseTabAsync({tab.Id}): tab watchdog Dispose() threw: {ex}"); }
        }

        if (wasActive && _tabs.Count > 0)
        {
            var next = _tabs[Math.Max(0, Math.Min(index, _tabs.Count - 1))];
            SelectTab(next);
        }
        else
        {
            RebuildTabStrip();
        }
        PersistOpenTabIds();
        await Task.CompletedTask;
    }

    private void PersistOpenTabIds()
    {
        _settings.OpenTabIds = _tabs.Select(t => t.Id).ToList();
        _settingsService.Save(_settings);
        SyncTabListToBackend();
    }

    /// <summary>
    /// Tab id + display name are otherwise known ONLY here (WPF's own
    /// _tabs/AppSettings.TabNames) -- the backend never sees a tab's name,
    /// just the bare tabId a WebView2 connects with. Per explicit
    /// instruction (2026-09-10, the Android companion app's UI needs a
    /// dynamic, never-hardcoded tab directory to render its own tab bar
    /// from): mirror the CURRENT full tab list to the backend on every
    /// add/close/rename, via the same /api/control channel every other
    /// WPF->backend control op already uses. Fire-and-forget -- a failure
    /// here must never block the tab strip UI; the backend treats this as
    /// best-effort (silently no-ops while not logged into SquirrelWisdom,
    /// same as every other companion-app sync).
    /// </summary>
    private async void SyncTabListToBackend()
    {
        try
        {
            var tabs = _tabs.Select(t => new { id = t.Id, name = t.Name }).ToList();
            var payload = JsonSerializer.Serialize(new { op = "tab_list_set", tabs });
            using var content = new StringContent(payload, System.Text.Encoding.UTF8, "application/json");
            using var resp = await _statusHttp.PostAsync($"http://127.0.0.1:{BackendProcess.Port}/api/control", content);
            if (!resp.IsSuccessStatusCode)
            {
                Logger.Log($"MainWindow.SyncTabListToBackend: /api/control returned {(int)resp.StatusCode}");
            }
        }
        catch (Exception ex)
        {
            Logger.Log($"MainWindow.SyncTabListToBackend: failed (ignored): {ex}");
        }
    }

    /// <summary>
    /// In-place rename: swaps the header's Button for a pre-filled, selected
    /// TextBox inside the SAME HeaderWrapper (same reuse constraint as
    /// RebuildTabStrip -- must not touch closeButton's parenting), committing
    /// on Enter/lost focus and discarding on Escape. Persisted immediately
    /// (AppSettings.TabNames), same pattern as PersistOpenTabIds.
    /// </summary>
    private void BeginRenameTab(ChatTab tab)
    {
        var editBox = new System.Windows.Controls.TextBox
        {
            Text = tab.Name,
            Padding = new Thickness(8, 5, 8, 5),
            Margin = new Thickness(0),
            MinWidth = 60,
        };

        var committed = false;
        void Commit(bool save)
        {
            // Guards against a double-commit: Escape/Enter already swap
            // editBox back out for headerButton, which itself triggers
            // editBox's LostFocus -- without this, that second call would
            // try to re-add headerButton while it's already a child (same
            // "already the logical child" exception RebuildTabStrip's reuse
            // discipline exists to avoid).
            if (committed) return;
            committed = true;
            tab.HeaderWrapper.Children.Remove(editBox);
            tab.HeaderWrapper.Children.Add(tab.HeaderButton);

            if (save)
            {
                var newName = editBox.Text.Trim();
                if (newName.Length > 0 && newName != tab.Name)
                {
                    tab.Name = newName;
                    tab.HeaderButton.Content = newName;
                    _settings.TabNames[tab.Id] = newName;
                    _settingsService.Save(_settings);
                    SyncTabListToBackend();
                    Logger.Log($"MainWindow.BeginRenameTab: tab {tab.Id} renamed to \"{newName}\"");
                }
            }
        }

        editBox.KeyDown += (_, e) =>
        {
            if (e.Key == System.Windows.Input.Key.Enter) { Commit(save: true); e.Handled = true; }
            else if (e.Key == System.Windows.Input.Key.Escape) { Commit(save: false); e.Handled = true; }
        };
        editBox.LostFocus += (_, _) => Commit(save: true);

        tab.HeaderWrapper.Children.Remove(tab.HeaderButton);
        tab.HeaderWrapper.Children.Add(editBox);
        editBox.Focus();
        editBox.SelectAll();
    }

    private void ShowError(string message)
    {
        SplashText.Text = message;
        Splash.Visibility = Visibility.Visible;
    }

    private void OnWebMessageReceived(WebView2 webView, CoreWebView2WebMessageReceivedEventArgs args)
    {
        try
        {
            var json = args.WebMessageAsJson;
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            var type = root.GetProperty("type").GetString();
            Logger.Log($"MainWindow: OnWebMessageReceived type={type ?? "null"}");

            if (type == "client_log")
            {
                // Bug fix (2026-09-10): chat.js's own console.log/error never
                // reached caroline.log at all before this -- see chat.js's
                // clog() for why. Written with a distinct "chat.js:" prefix
                // (not "MainWindow:") so it reads as the client's own voice
                // in the combined log, not this class's.
                var tabIdForLog = root.TryGetProperty("tabId", out var t) ? t.GetString() : "?";
                var clientMessage = root.TryGetProperty("message", out var m) ? m.GetString() : "";
                Logger.Log($"chat.js[tab={tabIdForLog}]: {clientMessage}");
                return;
            }

            if (type == "open_login")
            {
                OnOpenLogin(webView, root);
                return;
            }

            if (type == "open_payment")
            {
                OnOpenPayment(webView, root);
                return;
            }

            if (type == "set_always_on_top")
            {
                var value = root.GetProperty("value").GetBoolean();
                Logger.Log($"MainWindow: set_always_on_top value={value}");
                _settings.AlwaysOnTop = value;
                Topmost = value;
                _settingsService.Save(_settings);
                return;
            }

            if (type == "visual_mode_config")
            {
                // Once per backend-process lifetime (see server.ts's OutEvent doc comment) --
                // warms the resolved model (or does nothing if null, e.g. disabled/custom profile).
                var modelPath = root.TryGetProperty("modelPath", out var mp) ? mp.GetString() : null;
                Logger.Log($"MainWindow: visual_mode_config modelPath={modelPath ?? "null"}");
                _visualMode.Configure(modelPath);
                return;
            }

            if (type == "visual_speech_start")
            {
                // Wire key is "requestId" -- matches what chat.js actually sends (see
                // playOneSpeech's postMessage calls). Kept as the local var name
                // "visReqId" only to avoid the C# scope collision with a later
                // unrelated `requestId` declaration further down this method; that
                // rename previously (and wrongly) also changed the JSON property key
                // read here to "visReqId", which chat.js never sent -- every
                // visual_speech_* message from JS to native was silently failing on
                // GetProperty before ever reaching VisualModeManager. Confirmed live
                // (2026-09-03) as the actual reason Visual Mode never worked.
                var visReqId = root.GetProperty("requestId").GetString()!;
                _ = RunVisualModeAsync(() => _visualMode.HandleStartAsync(visReqId), $"visual_speech_start({visReqId})");
                return;
            }

            if (type == "visual_speech_audio")
            {
                var visReqId = root.GetProperty("requestId").GetString()!;
                var audioBytes = Convert.FromBase64String(root.GetProperty("audioBase64").GetString()!);
                _ = RunVisualModeAsync(async () =>
                {
                    var played = await _visualMode.HandleAudioAsync(visReqId, audioBytes);
                    // playOneSpeech (chat.js) awaits this to advance the speech queue.
                    // played=false tells it to fall back to plain audio playback instead
                    // of silently moving on with nothing having been heard (see
                    // HandleAudioAsync's own doc comment for why this can happen -- most
                    // commonly the model still warming up right after a fresh restart).
                    // Key is "requestId" here too -- see visual_speech_start's comment.
                    Logger.Log($"MainWindow: posting visual_speech_done requestId={visReqId} played={played}");
                    var payload = JsonSerializer.Serialize(new { type = "visual_speech_done", requestId = visReqId, played });
                    webView.CoreWebView2.PostWebMessageAsJson(payload);
                }, $"visual_speech_audio({visReqId})");
                return;
            }

            if (type == "visual_speech_stop")
            {
                var visReqId = root.GetProperty("requestId").GetString()!;
                _ = RunVisualModeAsync(() => _visualMode.HandleStopAsync(visReqId), $"visual_speech_stop({visReqId})");
                return;
            }

            if (type == "visual_speech_cancel")
            {
                var visReqId = root.GetProperty("requestId").GetString()!;
                _ = RunVisualModeAsync(() => _visualMode.HandleCancelAsync(visReqId), $"visual_speech_cancel({visReqId})");
                return;
            }

            if (type != "open_editor" && type != "open_office_editor" && type != "close_editor") return;
            var path = root.GetProperty("path").GetString()!;

            if (type == "close_editor")
            {
                // Caroline closing a viewer window herself (close_viewer
                // tool), not the user -- same as if they'd clicked Cancel/X.
                Logger.Log($"MainWindow: close_editor path={path} hadOpenWindow={_viewerWindows.ContainsKey(path)}");
                if (_viewerWindows.TryGetValue(path, out var existing)) existing.Close();
                return;
            }

            var requestId = root.GetProperty("requestId").GetString()!;
            Logger.Log($"MainWindow: {type} requestId={requestId} path={path}");

            Action<ViewerOutcome, string?> onDone = (outcome, resultPath) =>
            {
                Dispatcher.Invoke(() =>
                {
                    if (resultPath != null) _viewerWindows.Remove(resultPath);
                    var outcomeStr = outcome switch
                    {
                        ViewerOutcome.Saved => "saved",
                        ViewerOutcome.Cancelled => "cancelled",
                        ViewerOutcome.Closed => "closed",
                        _ => "error",
                    };
                    var payload = JsonSerializer.Serialize(new { type = "editor_result", requestId, outcome = outcomeStr, path = resultPath });
                    webView.CoreWebView2.PostWebMessageAsJson(payload);
                });
            };

            DocumentViewerWindow viewer;
            if (type == "open_office_editor")
            {
                var cfg = root.GetProperty("config");
                var config = new OfficeEditorConfig(
                    cfg.GetProperty("documentType").GetString()!,
                    cfg.GetProperty("fileType").GetString()!,
                    cfg.GetProperty("editable").GetBoolean(),
                    cfg.GetProperty("key").GetString()!,
                    cfg.GetProperty("documentUrl").GetString()!,
                    cfg.GetProperty("onlyofficeUrl").GetString()!,
                    cfg.GetProperty("title").GetString()!,
                    cfg.TryGetProperty("callbackUrl", out var cb) ? cb.GetString() : null);
                viewer = new DocumentViewerWindow(path, config, onDone);
            }
            else
            {
                var kind = root.GetProperty("kind").GetString()!;
                viewer = new DocumentViewerWindow(path, kind, onDone);
            }
            _viewerWindows[path] = viewer;
            viewer.Show();
        }
        catch (Exception ex)
        {
            Logger.Log($"OnWebMessageReceived failed: {ex}");
        }
    }

    /// <summary>Fire-and-forget wrapper for the async VisualModeManager calls above -- OnWebMessageReceived itself stays synchronous (matches every other case here), this just makes sure a failure logs instead of becoming an unobserved exception.</summary>
    private static async Task RunVisualModeAsync(Func<Task> action, string label)
    {
        try
        {
            await action();
        }
        catch (Exception ex)
        {
            Logger.Log($"VisualModeManager: {label} failed: {ex}");
        }
    }

    /// <summary>
    /// Opens (or reopens, with an error message, after a failed attempt --
    /// see server.ts's "login_submit" handler) Caroline's SquirrelWisdom
    /// login form. Keyed in _viewerWindows the same way file-based viewers
    /// are, under a fixed pseudo-path so at most one login window is ever
    /// open at a time -- shared across tabs (one SquirrelWisdom account
    /// either way), not per-tab.
    /// </summary>
    private const string LoginWindowKey = "squirrelwisdom-login";

    private void OnOpenLogin(WebView2 webView, JsonElement root)
    {
        var requestId = root.GetProperty("requestId").GetString()!;
        var error = root.TryGetProperty("error", out var e) ? e.GetString() : null;
        var noAiAtAll = root.TryGetProperty("noAiAtAll", out var n) && n.GetBoolean();
        Logger.Log($"MainWindow: OnOpenLogin requestId={requestId} error={error ?? "none"} noAiAtAll={noAiAtAll}");

        if (_viewerWindows.TryGetValue(LoginWindowKey, out var existing)) existing.Close();

        var viewer = new DocumentViewerWindow(error, noAiAtAll, (email, password, cancelled, isRegister, openSettingsInstead) =>
        {
            Dispatcher.Invoke(() =>
            {
                Logger.Log($"MainWindow: login form closed requestId={requestId} cancelled={cancelled} isRegister={isRegister} openSettingsInstead={openSettingsInstead}");
                _viewerWindows.Remove(LoginWindowKey);
                var payload = JsonSerializer.Serialize(new { type = "login_result", requestId, email, password, cancelled, isRegister });
                webView.CoreWebView2.PostWebMessageAsJson(payload);
                if (openSettingsInstead)
                {
                    _ = _activeTab?.WebView?.CoreWebView2?.ExecuteScriptAsync("window.carolineOpenSettings && window.carolineOpenSettings();");
                }
            });
        });
        _viewerWindows[LoginWindowKey] = viewer;
        viewer.Show();
    }

    private const string PaymentWindowKey = "squirrelwisdom-payment";

    private void OnOpenPayment(WebView2 webView, JsonElement root)
    {
        var checkoutUrl = root.GetProperty("checkoutUrl").GetString()!;
        Logger.Log($"MainWindow: OnOpenPayment checkoutUrl={checkoutUrl}");

        if (_viewerWindows.TryGetValue(PaymentWindowKey, out var existing)) existing.Close();

        var viewer = new DocumentViewerWindow(checkoutUrl, "payment", (outcome, path) =>
        {
            Dispatcher.Invoke(() =>
            {
                Logger.Log($"MainWindow: payment window closed outcome={outcome}");
                _viewerWindows.Remove(PaymentWindowKey);
                var payload = JsonSerializer.Serialize(new { type = "payment_result" });
                webView.CoreWebView2.PostWebMessageAsJson(payload);
            });
        });
        _viewerWindows[PaymentWindowKey] = viewer;
        viewer.Show();
    }

    private void ShowAndActivate()
    {
        Show();
        if (WindowState == WindowState.Minimized) WindowState = WindowState.Normal;
        Activate();
    }

    private void ToggleVisibility()
    {
        if (IsVisible && WindowState != WindowState.Minimized) Hide();
        else ShowAndActivate();
    }

    private void RequestExit()
    {
        _exitRequested = true;
        Close();
        ExitRequested?.Invoke(this, EventArgs.Empty);
    }

    private void OnClosing(object? sender, System.ComponentModel.CancelEventArgs e)
    {
        if (_exitRequested) return;
        e.Cancel = true;
        Hide();
    }

    /// <summary>Called once from App.OnExit -- persists window geometry and tears down the tray icon, hotkey, and backend process.</summary>
    public void Cleanup()
    {
        _settings.WindowLeft = Left;
        _settings.WindowTop = Top;
        _settings.WindowWidth = Width;
        _settings.WindowHeight = Height;
        _settingsService.Save(_settings);

        _tray.Dispose();
        _hotkey?.Dispose();
        _healthWatchdog?.Dispose();
        foreach (var tabWatchdog in _tabWatchdogs.Values)
        {
            try { tabWatchdog.Dispose(); }
            catch (Exception ex) { Logger.Log($"MainWindow shutdown: tab watchdog Dispose() threw (ignored, exiting anyway): {ex.Message}"); }
        }
        _tabWatchdogs.Clear();
        _statusHttp.Dispose();
        _appBrowserHost.Dispose();
        foreach (var tab in _tabs)
        {
            try { tab.WebView?.Dispose(); }
            catch (Exception ex) { Logger.Log($"MainWindow shutdown: WebView.Dispose() threw for tab {tab.Id} (ignored, exiting anyway): {ex.Message}"); }
        }
        _backend.Dispose();
    }
}

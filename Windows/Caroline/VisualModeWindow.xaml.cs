using System;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using Caroline.Services;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

namespace Caroline;

/// <summary>
/// Borderless, fully transparent, topmost window showing Visual Mode's
/// talking-head animation: appears bottom-right, just above the tray, with a
/// static "silence" frame while TTS/render are in flight, then swaps to the
/// rendered .webm once ready. Owns three native controls (pause/restart/stop)
/// -- see VisualModeManager for the queueing/render logic this window is
/// just a dumb player for.
/// </summary>
public partial class VisualModeWindow : Window
{
    private const int ScreenMargin = 12;
    /// <summary>Per explicit instruction (2026-09-03): a clip shorter than this doesn't get
    /// the pause/restart/stop bar at all -- not worth showing controls for something that
    /// short. Compared against the real rendered clip's duration (visualmode.html's own
    /// "loadedmetadata" event), not the source audio length.</summary>
    private const double ControlsMinDurationSeconds = 10.0;
    private WebView2? _webView;
    private bool _ready;

    /// <summary>Fired when the video finishes playing on its own (not via Stop).</summary>
    public event Action? PlaybackEnded;

    public VisualModeWindow()
    {
        InitializeComponent();
        Logger.Log("VisualModeWindow: constructed");
        Loaded += (_, _) =>
        {
            Logger.Log($"VisualModeWindow: Loaded event -- IsVisible={IsVisible} ActualWidth={ActualWidth} ActualHeight={ActualHeight}");
            PositionBottomRight();
        };
    }

    /// <summary>
    /// Show()/hide via Opacity, NOT Visibility -- the window must stay "shown" (a real HWND)
    /// the whole time so WebView2 can attach to it (see InitializeAsync's own doc comment on
    /// the Show-before-Init ordering fix); Opacity=0 just makes it visually invisible without
    /// tearing down the HWND. Per explicit instruction (2026-09-03): the static "silence"
    /// frame turned out not to be worth showing during the TTS/render wait -- the window
    /// should only actually be visible once real playback starts.
    /// </summary>
    public void SetVisuallyHidden(bool hidden)
    {
        Opacity = hidden ? 0 : 1;
        Logger.Log($"VisualModeWindow: SetVisuallyHidden({hidden}) -- Opacity={Opacity}");
    }

    private void PositionBottomRight()
    {
        var wa = SystemParameters.WorkArea; // excludes the taskbar -- "above the tray" for free
        Left = wa.Right - Width - ScreenMargin;
        Top = wa.Bottom - Height - ScreenMargin;
        Logger.Log($"VisualModeWindow: PositionBottomRight -- WorkArea={wa} -> Left={Left} Top={Top}");
    }

    // Same reasoning as AppBrowserWindow's own InitTimeout (see its doc comment):
    // confirmed live (2026-09-03) that this window's EnsureCoreWebView2Async call
    // had NO timeout at all, unlike every other WebView2 init in this app -- it hung
    // forever (54+s and counting, never resolved), leaving _ready permanently false.
    // Worse, VisualModeManager.HandleAudioAsync had no way to know that: it rendered
    // the full video anyway (30+s of real work, wasted) and then awaited
    // playbackEnded forever too, since a never-ready window's PlayVideoAsync silently
    // no-ops instead of ever firing the "ended"/TriggerStop signal -- so the ENTIRE
    // speech queue (this reply AND every subsequent one, visual or not) hung with it.
    // This is very likely why voice output looked completely broken, not just Visual
    // Mode specifically.
    private static readonly TimeSpan InitTimeout = TimeSpan.FromSeconds(60);

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

    /// <summary>True once EnsureCoreWebView2Async + navigation have both actually
    /// completed -- callers (VisualModeManager.HandleAudioAsync) must check this
    /// BEFORE attempting to render/play into this window, not just whether the
    /// window object itself is non-null.</summary>
    public bool IsReady => _ready;

    public async Task InitializeAsync()
    {
        Logger.Log("VisualModeWindow: InitializeAsync entered");
        var webView = new WebView2();
        WebViewHost.Children.Add(webView);
        // Must be set before EnsureCoreWebView2Async -- see WebView2's own docs.
        // This (plus the page's own "background: transparent" CSS) is what makes
        // the rendered video's alpha channel actually show through as a
        // transparent WINDOW, not just a transparent-looking black rectangle.
        webView.DefaultBackgroundColor = System.Drawing.Color.Transparent;
        // Dedicated profile dir -- confirmed live (2026-09-03) this was the only
        // WebView2 usage in the app calling EnsureCoreWebView2Async() with NO
        // explicit userDataFolder/environment, unlike AppBrowserWindow (per-label
        // dirs) and MainWindow's chat tabs (a shared explicit "webview2" dir).
        // Giving it its own profile, same convention as everywhere else, rather
        // than falling back to WebView2's own implicit default.
        var dataDir = System.IO.Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "Caroline", "webview2-visualmode");
        var env = await TimeoutAfter(
            CoreWebView2Environment.CreateAsync(userDataFolder: dataDir), InitTimeout, "VisualModeWindow CoreWebView2Environment.CreateAsync");
        await TimeoutAfter(webView.EnsureCoreWebView2Async(env), InitTimeout, "VisualModeWindow EnsureCoreWebView2Async");
        Logger.Log("VisualModeWindow: EnsureCoreWebView2Async done");

        var htmlPath = System.IO.Path.Combine(AppContext.BaseDirectory, "wwwroot", "visualmode.html");
        var navUri = new Uri(htmlPath).AbsoluteUri;
        Logger.Log($"VisualModeWindow: navigating to {navUri}");
        webView.CoreWebView2.Navigate(navUri);

        var tcs = new TaskCompletionSource();
        void OnNavCompleted(object? s, CoreWebView2NavigationCompletedEventArgs e)
        {
            Logger.Log($"VisualModeWindow: NavigationCompleted success={e.IsSuccess} status={e.WebErrorStatus}");
            webView.CoreWebView2.NavigationCompleted -= OnNavCompleted;
            tcs.TrySetResult();
        }
        webView.CoreWebView2.NavigationCompleted += OnNavCompleted;
        await tcs.Task;
        Logger.Log("VisualModeWindow: InitializeAsync done");

        webView.CoreWebView2.WebMessageReceived += (_, args) =>
        {
            Logger.Log($"VisualModeWindow: WebMessageReceived raw={args.WebMessageAsJson}");
            try
            {
                using var doc = JsonDocument.Parse(args.WebMessageAsJson);
                var type = doc.RootElement.GetProperty("type").GetString();
                if (type == "ended")
                {
                    PlaybackEnded?.Invoke();
                }
                else if (type == "duration")
                {
                    var seconds = doc.RootElement.GetProperty("seconds").GetDouble();
                    Logger.Log($"VisualModeWindow: duration={seconds:F1}s -- {(seconds < ControlsMinDurationSeconds ? "hiding" : "showing")} controls bar");
                    ControlsBar.Visibility = seconds < ControlsMinDurationSeconds ? Visibility.Collapsed : Visibility.Visible;
                }
            }
            catch (Exception ex)
            {
                Logger.Log($"VisualModeWindow: WebMessageReceived parse failed: {ex}");
            }
        };

        _webView = webView;
        _ready = true;
    }

    /// <summary>Shows the "silence" frame -- dataUri is a data:image/png;base64,... string.</summary>
    public async Task ShowStaticAsync(string dataUri)
    {
        Logger.Log($"VisualModeWindow: ShowStaticAsync entered (ready={_ready}, hasWebView={_webView?.CoreWebView2 != null}, dataUriLength={dataUri.Length})");
        if (!_ready || _webView?.CoreWebView2 == null)
        {
            Logger.Log("VisualModeWindow: ShowStaticAsync -- not ready, skipping");
            return;
        }
        var result = await _webView.CoreWebView2.ExecuteScriptAsync($"window.showStatic({JsonSerializer.Serialize(dataUri)})");
        Logger.Log($"VisualModeWindow: ShowStaticAsync ExecuteScriptAsync result={result}, IsVisible={IsVisible}");
    }

    /// <summary>Swaps to the rendered video -- webmPath is a local file path.</summary>
    public async Task PlayVideoAsync(string webmPath)
    {
        Logger.Log($"VisualModeWindow: PlayVideoAsync entered (ready={_ready}, hasWebView={_webView?.CoreWebView2 != null}, path={webmPath}, fileExists={System.IO.File.Exists(webmPath)})");
        if (!_ready || _webView?.CoreWebView2 == null)
        {
            Logger.Log("VisualModeWindow: PlayVideoAsync -- not ready, skipping");
            return;
        }
        var fileUri = new Uri(webmPath).AbsoluteUri;
        var result = await _webView.CoreWebView2.ExecuteScriptAsync($"window.playVideo({JsonSerializer.Serialize(fileUri)})");
        Logger.Log($"VisualModeWindow: PlayVideoAsync ExecuteScriptAsync result={result}, IsVisible={IsVisible}");
    }

    private void OnPauseClick(object sender, RoutedEventArgs e)
    {
        Logger.Log("VisualModeWindow: OnPauseClick (user toggled pause/play)");
        _webView?.CoreWebView2?.ExecuteScriptAsync(
            "(function(){var v=document.getElementById('vid'); if(v.paused) v.play(); else v.pause();})()");
    }

    private void OnRestartClick(object sender, RoutedEventArgs e)
    {
        Logger.Log("VisualModeWindow: OnRestartClick (user restarted playback)");
        _webView?.CoreWebView2?.ExecuteScriptAsync(
            "(function(){var v=document.getElementById('vid'); v.currentTime=0; v.play().catch(()=>{});})()");
    }

    private void OnStopClick(object sender, RoutedEventArgs e)
    {
        Logger.Log("VisualModeWindow: OnStopClick (user stopped playback)");
        // Confirmed live (2026-09-03) as a real bug: unlike Pause/Restart above, this used
        // to only call TriggerStop() -- which unblocks VisualModeManager's own await and
        // eventually closes this window, but does nothing to the still-playing <video>
        // element itself in the meantime. Audio (muxed into the .webm) kept playing for
        // several more seconds until the window-close teardown got around to actually
        // tearing down the WebView2 control. Stopping the video directly, immediately,
        // fixes that -- same idea as Pause/Restart, just also clearing `src` so nothing
        // keeps decoding/outputting audio in the background at all.
        _webView?.CoreWebView2?.ExecuteScriptAsync(
            "(function(){var v=document.getElementById('vid'); v.pause(); v.removeAttribute('src'); v.load();})()");
        TriggerStop();
    }

    /// <summary>
    /// Manual stop -- from this window's own Stop button, or relayed via
    /// VisualModeManager.HandleStopAsync (the main chat window's Stop button /
    /// queue-clear). Treated the same as the video ending on its own:
    /// VisualModeManager doesn't distinguish "finished" from "stopped", both
    /// just mean "done, unblock whatever's awaiting playbackEnded".
    /// </summary>
    public void TriggerStop() => PlaybackEnded?.Invoke();
}

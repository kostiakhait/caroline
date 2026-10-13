using System;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Threading;
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

    /// <summary>True when the clip playing now was cut short by this window's own Stop
    /// button. The chat side reads replies in pieces, one clip each: without this it only
    /// saw "the clip is over" and went on to the next piece of a reply the user had just
    /// stopped (VisualModeManager passes it on in visual_speech_done).</summary>
    public bool StoppedByUser { get; private set; }

    /// <summary>Set before each clip: more speech is queued behind it. A reply is read in
    /// pieces and its first piece is always a short one, so by ControlsMinDurationSeconds
    /// alone a minute-long reply began with ten seconds in which there was nothing to
    /// stop it with (2026-10-11, live: "нету кнопки отменить!!"). "Not worth showing
    /// controls for something that short" is about a short utterance, not about the short
    /// opening of a long one.</summary>
    public bool MoreToCome { get; set; }
    private bool _controlsShown;

    /// <summary>Stop was pressed while no clip was playing -- the window is standing
    /// between two remarks of a dialogue (see VisualModeManager.StayForNextClip).</summary>
    public event Action? DismissRequested;
    private bool _playing;

    /// <summary>This window has played a clip of a spoken dialogue, so it is the user who
    /// closes it, not a timer -- see VisualModeManager.StayForNextClip.</summary>
    public bool StaysUp { get; set; }

    /// <summary>Standing between two remarks: there is nothing to pause or to restart
    /// (Restart would play the last remark again, out of a file already deleted), only
    /// Stop, which sends the window away.</summary>
    public bool Idle
    {
        set
        {
            PauseBtn.Visibility = RestartBtn.Visibility = value ? Visibility.Collapsed : Visibility.Visible;
            if (value) ControlsBar.Visibility = Visibility.Visible;
        }
    }

    [DllImport("user32.dll")]
    private static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int x, int y, int cx, int cy, uint flags);
    private static readonly IntPtr HwndTopmost = new(-1);
    private const uint SwpNoSize = 0x0001, SwpNoMove = 0x0002, SwpNoActivate = 0x0010;

    // Bug fix (2026-10-11), confirmed live ("если на модельку случайно нажать, она
    // пропадёт и тупо не отменишь"): this window and the main chat window are BOTH
    // Topmost, and among topmost windows the one activated last is on top. A click on
    // the model -- or anywhere in the chat window, which sits right behind it -- left
    // the chat window in front: this one was still alive and still speaking, but
    // covered, its Stop button with it. Measured after one click on the model: the chat
    // window was the foreground window and one place above this one in the z-order.
    // Re-asserting the topmost position (without taking the focus) puts it back on top:
    // at once when either window is activated, and on a timer for anything else that
    // could get in front of it.
    private readonly DispatcherTimer _stayOnTopTimer = new() { Interval = TimeSpan.FromMilliseconds(300) };
    private readonly System.Collections.Generic.List<Window> _otherWindows = new();

    private void StayOnTop()
    {
        var hwnd = new WindowInteropHelper(this).Handle;
        if (hwnd != IntPtr.Zero) SetWindowPos(hwnd, HwndTopmost, 0, 0, 0, 0, SwpNoMove | SwpNoSize | SwpNoActivate);
    }

    private void OnOtherWindowActivated(object? sender, EventArgs e) => StayOnTop();

    public VisualModeWindow()
    {
        InitializeComponent();
        Logger.Log("VisualModeWindow: constructed");
        Loaded += (_, _) =>
        {
            Logger.Log($"VisualModeWindow: Loaded event -- IsVisible={IsVisible} ActualWidth={ActualWidth} ActualHeight={ActualHeight}");
            PositionBottomRight();
            _stayOnTopTimer.Start();
        };
        _stayOnTopTimer.Tick += (_, _) => StayOnTop();
        Deactivated += (_, _) => StayOnTop();
        if (System.Windows.Application.Current != null)
        {
            foreach (Window other in System.Windows.Application.Current.Windows)
            {
                if (other == this) continue;
                other.Activated += OnOtherWindowActivated;
                _otherWindows.Add(other);
            }
        }
        Closed += (_, _) =>
        {
            _stayOnTopTimer.Stop();
            foreach (var other in _otherWindows) other.Activated -= OnOtherWindowActivated;
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
        var left = wa.Right - Width - ScreenMargin;
        var top = wa.Bottom - Height - ScreenMargin;
        // A window that stays for a whole dialogue must not stand on the chat (2026-10-11):
        // docked at the right edge, the chat window has its input box, microphone and send
        // button exactly under this corner. Then the model stands just left of the chat
        // window instead, when there is room for it.
        foreach (Window other in System.Windows.Application.Current.Windows)
        {
            if (other is not MainWindow || !other.IsVisible || other.WindowState == WindowState.Minimized) continue;
            var overlaps = left < other.Left + other.ActualWidth && left + Width > other.Left
                           && top < other.Top + other.ActualHeight && top + Height > other.Top;
            var beside = other.Left - Width - ScreenMargin;
            if (overlaps && beside >= wa.Left) left = beside;
            break;
        }
        if (Math.Abs(Left - left) < 0.5 && Math.Abs(Top - top) < 0.5) return;
        Left = left;
        Top = top;
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
            if (!WebViewOriginGuard.IsTrustedSource(args.Source)) return;
            try
            {
                using var doc = JsonDocument.Parse(args.WebMessageAsJson);
                var type = doc.RootElement.GetProperty("type").GetString();
                if (type == "ended")
                {
                    _playing = false;
                    PlaybackEnded?.Invoke();
                }
                else if (type == "duration")
                {
                    var seconds = doc.RootElement.GetProperty("seconds").GetDouble();
                    // Once shown, the bar stays for the clips that follow in this window: it
                    // must not vanish under the cursor for a short last piece.
                    _controlsShown = _controlsShown || MoreToCome || seconds >= ControlsMinDurationSeconds;
                    Logger.Log($"VisualModeWindow: duration={seconds:F1}s moreToCome={MoreToCome} -- {(_controlsShown ? "showing" : "hiding")} controls bar");
                    ControlsBar.Visibility = _controlsShown ? Visibility.Visible : Visibility.Collapsed;
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
        StoppedByUser = false; // a window can play several clips in a row, see VisualModeManager.StartPostRoll
        _playing = true;
        Idle = false;
        PositionBottomRight(); // the chat window may have been moved since the last clip
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
        var wasPlaying = _playing;
        _playing = false;
        StoppedByUser = true;
        TriggerStop();
        if (!wasPlaying) DismissRequested?.Invoke();
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

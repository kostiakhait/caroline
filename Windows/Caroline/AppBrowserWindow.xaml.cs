using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Management;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows;
using System.Windows.Interop;
using Caroline.Native;
using Caroline.Services;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

namespace Caroline;

/// <summary>
/// One persistent, labeled embedded browser window (e.g. "whatsapp",
/// "telegram", "facebook", "slack") -- Caroline's own multi-window browser,
/// living inside the app instead of a separate standalone Chromium process
/// (see MCP/browser, which does the latter and is kept only as a fallback
/// for cases this can't handle). Each label gets its own persistent
/// WebView2 profile folder, so logging into WhatsApp Web in one window
/// doesn't touch Telegram's session in another.
///
/// Automation (snapshot/click/type) is implemented in plain JS via
/// ExecuteScriptAsync rather than CDP/Playwright -- WebView2 doesn't need a
/// separate debugging port or an external process at all this way. The
/// tagging/click/type approach mirrors MCP/browser/src/index.ts's own
/// (data-mcp-ref attributes, JS event-dispatch fallback for React-controlled
/// inputs) so behavior stays consistent between the embedded and standalone
/// tools -- see AppBrowserHost.cs for the HTTP bridge that lets the backend
/// (a plain Node process, not this WPF one) call into these methods.
/// </summary>
public partial class AppBrowserWindow : Window
{
    public string Label { get; }
    /// <summary>Remote-debugging (CDP) port this window's WebView2 will listen on once
    /// initialized -- see EnsureInitializedAsync. The Node backend connects here directly
    /// (playwright-core's connectOverCDP) for snapshot/find/click/type/evaluate, since CDP
    /// doesn't go through the page's own JS context and so isn't subject to its CSP the way
    /// ExecuteScriptAsync-injected script is -- confirmed live (2026-08-31) as a real gap:
    /// evaluate() was silently blocked by page CSP on some sites (ChatGPT, Facebook), and
    /// since snapshot/find/click/type all worked the same way under the hood, they were
    /// equally broken there, not just the standalone evaluate tool.</summary>
    public int CdpPort { get; }
    private WebView2? _webView;
    private bool _ready;

    /// <summary>Applied to every labeled browser window once, right after it
    /// initializes -- confirmed live (2026-09-05) that Caroline took 494
    /// screenshots in one night at 100% zoom and never once asked for a
    /// crop, despite app_browser_screenshot supporting one and a system
    /// prompt instruction asking her to prefer it -- instructions alone
    /// don't reliably change behavior a tool's own default doesn't nudge
    /// toward. Zooming the page out fits more real content into the exact
    /// same screenshot pixel dimensions, cutting how many separate
    /// screenshots/scrolls a given page needs. 0.75 is a fixed, conservative
    /// default (not adaptively tuned per page to "smallest still-readable"
    /// -- that would need actual OCR/legibility detection, out of scope for
    /// this pass) chosen to stay comfortably legible on typical web text
    /// sizes while still meaningfully shrinking the effective viewport.</summary>
    private const double DefaultZoomFactor = 0.75;

    /// <summary>Default cap applied in ScreenshotAsync when the caller
    /// doesn't specify maxWidth (and doesn't crop) -- previously an
    /// unspecified maxWidth meant NO downscaling at all, returning the
    /// capture at full native pixel width (which, at typical embedded
    /// window sizes, is far wider than needed for Claude's vision input and
    /// pure image-token waste). Still fully overridable by an explicit
    /// larger maxWidth when more detail is genuinely needed.</summary>
    private const int DefaultScreenshotMaxWidth = 1280;

    public AppBrowserWindow(string label, int cdpPort)
    {
        InitializeComponent();
        Label = label;
        CdpPort = cdpPort;
        TitleText.Text = $"Caroline Browser — {label}";
        Logger.Log($"[app-browser-window:{label}] constructed (cdpPort={cdpPort})");
    }

    /// <summary>
    /// Shows the window immediately -- deliberately separate from (and called
    /// BEFORE) EnsureInitializedAsync. Confirmed live (2026-08-31): the
    /// previous code called Show() only after WebView2 environment creation
    /// finished, so a slow/stuck first-run WebView2 init (a brand-new
    /// profile spins up its own runtime process) meant the window never
    /// appeared at all, with nothing to show for it but a silent timeout on
    /// the caller's end -- exactly the "у меня даже окно и не пробовало
    /// открыться" bug. The window should appear on screen right away
    /// (blank/loading is fine), same expectation as any other app window.
    /// </summary>
    public void ShowNow()
    {
        Logger.Log($"[app-browser-window:{Label}] ShowNow() entered (thread={Environment.CurrentManagedThreadId}, isUIThread={Dispatcher.CheckAccess()})");
        Show();
        Activate();
        Logger.Log($"[app-browser-window:{Label}] ShowNow() done -- window should be visible now");
    }

    // The 2026-09-01 fix below only bounded the wait -- it didn't touch what
    // was actually stuck. Root-caused for real on 2026-09-03 by reading
    // caroline.log across several repeated "facebook" hangs: CreateAsync
    // itself always returned in 0.0s every time; EnsureCoreWebView2Async was
    // what hung, and ONLY for the "facebook" profile dir -- a fresh profile
    // (a "fbcheck" label opened moments later, same machine, same load)
    // initialized in 0.3s. `wmic process ... get CommandLine` then showed why:
    // dozens of accumulated msedgewebview2.exe processes all pinned to the
    // same --user-data-dir=...\webview2-appbrowser-facebook\EBWebView,
    // spanning multiple launch attempts over hours. Chromium's profile
    // directory takes a single-instance lock (EBWebView\lockfile); the FIRST
    // stuck browser process for a label never gets killed by the timeout
    // below (it only stops US waiting, per its own doc comment), so it just
    // sits there holding the lock -- and EVERY subsequent retry for that same
    // label launches a brand-new browser process that blocks forever trying
    // to acquire a lock an old zombie is still holding, becoming one more
    // zombie itself. That's the real mechanism behind "already the third
    // time" and dozens of piled-up msedgewebview2.exe processes: not one
    // slow init, but a self-compounding pile of processes each waiting on
    // the one before it. KillStaleWebView2Processes below is the actual fix
    // -- clear out anything still holding this label's profile lock BEFORE
    // asking Chromium to launch a new browser process for it, so a retry
    // starts from a genuinely free lock instead of queuing behind a zombie.
    private static readonly TimeSpan InitTimeout = TimeSpan.FromSeconds(60);

    /// <summary>
    /// Kills any msedgewebview2.exe process (main browser process or one of its
    /// helper processes -- GPU, utility, renderer, crashpad-handler all carry the
    /// same --user-data-dir) still pinned to this label's profile directory.
    /// Command lines of OTHER processes aren't available via System.Diagnostics.Process
    /// on Windows, hence WMI (Win32_Process) here -- the one place in this app that
    /// needs it, so no broader dependency than the single System.Management package.
    /// Best-effort throughout: a process that already exited between the WMI query and
    /// the kill attempt just throws harmlessly, which we swallow.
    /// </summary>
    private static void KillStaleWebView2Processes(string dataDir)
    {
        var marker = "--user-data-dir=" + dataDir; // matches both quoted and unquoted forms as a substring
        try
        {
            using var searcher = new ManagementObjectSearcher(
                "SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name = 'msedgewebview2.exe'");
            using var results = searcher.Get();
            foreach (ManagementObject mo in results)
            {
                var commandLine = mo["CommandLine"] as string;
                if (string.IsNullOrEmpty(commandLine) || commandLine.IndexOf(marker, StringComparison.OrdinalIgnoreCase) < 0)
                    continue;
                var pid = (uint)mo["ProcessId"];
                try
                {
                    Process.GetProcessById((int)pid).Kill();
                    Logger.Log($"KillStaleWebView2Processes: killed stale msedgewebview2.exe pid={pid} for {dataDir}");
                }
                catch (Exception ex)
                {
                    Logger.Log($"KillStaleWebView2Processes: kill pid={pid} failed (likely already exited): {ex.Message}");
                }
            }
        }
        catch (Exception ex)
        {
            Logger.Log($"KillStaleWebView2Processes: WMI query failed: {ex}");
        }
    }

    public async Task EnsureInitializedAsync()
    {
        if (_ready)
        {
            Logger.Log($"[app-browser-window:{Label}] EnsureInitializedAsync: already ready, no-op");
            return;
        }
        var sw = Stopwatch.StartNew();
        Logger.Log($"[app-browser-window:{Label}] EnsureInitializedAsync: entered (thread={Environment.CurrentManagedThreadId}, isUIThread={Dispatcher.CheckAccess()})");
        var webView = new WebView2();
        WebViewHost.Children.Add(webView);
        _webView = webView;
        Logger.Log($"[app-browser-window:{Label}] EnsureInitializedAsync: WebView2 control created and added to visual tree ({sw.Elapsed.TotalSeconds:F1}s)");

        // Separate profile per label -- same reasoning as DocumentViewerWindow's
        // dedicated profile dirs (webview2-office, webview2-payment): distinct
        // login/cookie state per site, never sharing with the chat page's own.
        var dataDir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            "Caroline", "webview2-appbrowser-" + SanitizeForPath(Label));
        Logger.Log($"[app-browser-window:{Label}] EnsureInitializedAsync: calling CoreWebView2Environment.CreateAsync (profile dir: {dataDir}, cdpPort={CdpPort})...");
        // --remote-allow-origins=* -- required by modern Chromium's DevTools origin check,
        // otherwise a WebSocket CDP connection from playwright-core (not itself a browser tab)
        // gets rejected outright. Loopback-only port, same trust model as every other local
        // control surface in this app (AppBrowserHost, the backend's own /api/*) -- nothing
        // remote can reach it.
        var envOptions = new CoreWebView2EnvironmentOptions
        {
            AdditionalBrowserArguments = $"--remote-debugging-port={CdpPort} --remote-allow-origins=*",
        };
        // Clear out any browser process still holding this profile's lock from a
        // previous hung/abandoned attempt -- see InitTimeout's doc comment for why
        // this is the actual fix, not just the timeout below.
        KillStaleWebView2Processes(dataDir);
        CoreWebView2Environment env;
        try
        {
            env = await TimeoutAfter(
                CoreWebView2Environment.CreateAsync(userDataFolder: dataDir, options: envOptions),
                InitTimeout, $"CoreWebView2Environment.CreateAsync({Label})");
            Logger.Log($"[app-browser-window:{Label}] EnsureInitializedAsync: CoreWebView2Environment.CreateAsync returned ({sw.Elapsed.TotalSeconds:F1}s total) -- calling EnsureCoreWebView2Async...");
            await TimeoutAfter(
                webView.EnsureCoreWebView2Async(env), InitTimeout, $"EnsureCoreWebView2Async({Label})");
        }
        catch (TimeoutException)
        {
            // This attempt's own browser process is now the zombie holding the lock --
            // clear it immediately rather than leaving it for the next retry to find.
            KillStaleWebView2Processes(dataDir);
            throw;
        }
        Logger.Log($"[app-browser-window:{Label}] EnsureInitializedAsync: EnsureCoreWebView2Async returned ({sw.Elapsed.TotalSeconds:F1}s total)");
        webView.ZoomFactor = DefaultZoomFactor;
        Logger.Log($"[app-browser-window:{Label}] EnsureInitializedAsync: set ZoomFactor={DefaultZoomFactor}");
        webView.CoreWebView2.NavigationCompleted += (_, args) =>
        {
            Logger.Log($"[app-browser-window:{Label}] NavigationCompleted: success={args.IsSuccess} status={args.WebErrorStatus} url={webView.Source}");
            Dispatcher.Invoke(() => UrlText.Text = webView.Source?.ToString() ?? "");
        };
        _ready = true;
        Logger.Log($"[app-browser-window:{Label}] EnsureInitializedAsync: done, ready ({sw.Elapsed.TotalSeconds:F1}s total)");
    }

    /// <summary>Races any Task against a deadline -- on timeout, throws instead of leaving
    /// the caller awaiting forever. Does not (cannot) actually cancel the underlying
    /// native operation; it just stops OUR side from waiting on it indefinitely, which is
    /// what actually matters to the caller (a real error beats a permanent hang).</summary>
    private static async Task TimeoutAfter(Task task, TimeSpan timeout, string what)
    {
        var completed = await Task.WhenAny(task, Task.Delay(timeout));
        if (completed != task)
        {
            throw new TimeoutException($"{what} did not complete within {timeout.TotalSeconds:F0}s.");
        }
        await task; // propagate a real exception from the task itself, if it had one
    }

    private static async Task<T> TimeoutAfter<T>(Task<T> task, TimeSpan timeout, string what)
    {
        var completed = await Task.WhenAny(task, Task.Delay(timeout));
        if (completed != task)
        {
            throw new TimeoutException($"{what} did not complete within {timeout.TotalSeconds:F0}s.");
        }
        return await task;
    }

    /// <summary>
    /// Starts navigation without waiting for it to finish -- used for the
    /// initial URL passed to open_app_browser, so opening the window doesn't
    /// block on a full page load (a heavy SPA like WhatsApp Web can take a
    /// long time to fire NavigationCompleted, especially under system load;
    /// the window itself should be usable/visible immediately regardless).
    /// See NavigateAsync below for the blocking version, used by the
    /// explicit app_browser_navigate tool where waiting for completion is
    /// actually the point.
    /// </summary>
    public async Task StartNavigateAsync(string url)
    {
        await EnsureInitializedAsync();
        Logger.Log($"[app-browser-window:{Label}] StartNavigateAsync: navigating to {url} (not waiting for completion)");
        _webView!.CoreWebView2.Navigate(url);
    }

    private static string SanitizeForPath(string label)
    {
        var chars = label.ToLowerInvariant().Where(c => char.IsLetterOrDigit(c) || c == '-').ToArray();
        return chars.Length > 0 ? new string(chars) : "default";
    }

    public async Task NavigateAsync(string url)
    {
        await EnsureInitializedAsync();
        var sw = Stopwatch.StartNew();
        Logger.Log($"[app-browser-window:{Label}] NavigateAsync: navigating to {url}, waiting for completion...");
        var tcs = new TaskCompletionSource();
        void Handler(object? s, CoreWebView2NavigationCompletedEventArgs e)
        {
            _webView!.CoreWebView2.NavigationCompleted -= Handler;
            tcs.TrySetResult();
        }
        _webView!.CoreWebView2.NavigationCompleted += Handler;
        _webView.CoreWebView2.Navigate(url);
        await tcs.Task;
        Logger.Log($"[app-browser-window:{Label}] NavigateAsync: completed after {sw.Elapsed.TotalSeconds:F1}s");
    }

    // SnapshotAsync/FindAsync/ClickAsync/TypeAsync/PressKeyAsync/EvaluateAsync
    // (the JS-injection/ExecuteScriptAsync versions) moved to the Node side
    // (appBrowserCdp.ts), which connects over real CDP via playwright-core
    // instead -- confirmed live (2026-08-31) that ExecuteScriptAsync-injected
    // script is silently blocked by page CSP on some sites (ChatGPT,
    // Facebook: even `() => 42` failed), and CDP's Runtime.evaluate is NOT
    // subject to page CSP the same way, since it's injected at the debugger
    // protocol level rather than as page-context script. This window still
    // owns everything that ISN'T page-content JS: window lifecycle,
    // screenshots (CapturePreviewAsync, also not CSP-gated), and the real
    // OS-level input methods below (SendInput is native input, has nothing
    // to do with the page's script context at all).

    private static string TargetSelectorJs(string? refOrNull, string? selectorOrNull)
    {
        if (!string.IsNullOrEmpty(refOrNull))
            return JsonSerializer.Serialize($"[data-mcp-ref=\"{refOrNull}\"]");
        if (!string.IsNullOrEmpty(selectorOrNull))
            return JsonSerializer.Serialize(selectorOrNull);
        throw new ArgumentException("Provide either ref (from snapshot/find) or selector.");
    }

    /// <summary>
    /// Element center in viewport (CSS-px) coordinates, or null if not found. Shared by the
    /// Real* methods below -- they need the actual on-screen position, not just a selector, to
    /// drive a real OS-level click/keystroke via RealInput.
    /// </summary>
    private async Task<(double x, double y)?> GetElementCenterInViewportAsync(string? refValue, string? selector)
    {
        var selJs = TargetSelectorJs(refValue, selector);
        var script = $$"""
            (() => {
              const el = document.querySelector({{selJs}});
              if (!el) return null;
              el.scrollIntoView({block:'center'});
              const r = el.getBoundingClientRect();
              return {x: r.x + r.width/2, y: r.y + r.height/2};
            })()
            """;
        var raw = await _webView!.CoreWebView2.ExecuteScriptAsync(script);
        var point = JsonSerializer.Deserialize<JsonElement>(raw);
        if (point.ValueKind != JsonValueKind.Object) return null;
        return (point.GetProperty("x").GetDouble(), point.GetProperty("y").GetDouble());
    }

    /// <summary>Viewport (CSS-px) point -> real screen point, via this WebView2's own
    /// PointToScreen -- the one conversion every coordinate-based Real* method needs.</summary>
    private System.Windows.Point ViewportToScreen(double viewportX, double viewportY) =>
        _webView!.PointToScreen(new System.Windows.Point(viewportX, viewportY));

    /// <summary>
    /// Real OS-level click (SetCursorPos + mouse_event via RealInput) instead of a JS-dispatched
    /// one -- for isTrusted-gated sites (WhatsApp Web among them, confirmed live 2026-08-31) that
    /// silently ignore synthetic events on protected actions. Physically moves the real system
    /// cursor and needs this window focused/visible/unobscured -- a real side effect, not free
    /// the way the JS-dispatch ClickAsync is; use only when that one doesn't actually register.
    /// </summary>
    public async Task<string> ClickRealAsync(string? refValue, string? selector)
    {
        await EnsureInitializedAsync();
        Logger.Log($"[app-browser-window:{Label}] ClickRealAsync: ref={refValue} selector={selector}");
        var center = await GetElementCenterInViewportAsync(refValue, selector);
        if (center is null)
        {
            Logger.Log($"[app-browser-window:{Label}] ClickRealAsync: no matching element");
            return "no-element";
        }
        return await ClickAtPointAsync(center.Value.x, center.Value.y);
    }

    /// <summary>
    /// Real click at a raw viewport (CSS-px) coordinate, no DOM lookup at all -- for when the
    /// accessibility snapshot doesn't see the target (confirmed live 2026-08-31: some messenger
    /// web apps render message bubbles in a way that never shows up in a snapshot/find result).
    /// Caroline can look at app_browser_screenshot herself (she's multimodal) and click wherever
    /// she actually sees the target, the same way a human would, without needing OCR or a
    /// separate vision tool -- the screenshot's pixel coordinates ARE viewport coordinates.
    /// </summary>
    public async Task<string> ClickAtPointAsync(double viewportX, double viewportY)
    {
        await EnsureInitializedAsync();
        var screenPoint = ViewportToScreen(viewportX, viewportY);
        Logger.Log($"[app-browser-window:{Label}] ClickAtPointAsync: viewport=({viewportX:F0},{viewportY:F0}) -> screen=({screenPoint.X:F0},{screenPoint.Y:F0})");
        ShowNow();
        await Task.Delay(80); // let activation actually settle before the click lands
        RealInput.Click((int)screenPoint.X, (int)screenPoint.Y);
        Logger.Log($"[app-browser-window:{Label}] ClickAtPointAsync: done");
        return "clicked (real OS input)";
    }

    /// <summary>
    /// Real mouse-wheel scroll targeting a specific point (either a resolved element's center,
    /// via ref/selector, or a raw viewport coordinate) -- fixes a real, repeatedly-hit failure
    /// mode: a page-level PageUp/PageDown scrolls whatever currently has focus, which in a SPA
    /// wanders unpredictably, so it often scrolls the wrong pane entirely instead of the intended
    /// inner message list. The OS routes wheel events by cursor position, not focus, so aiming at
    /// a specific point sidesteps that ambiguity completely.
    /// </summary>
    public async Task<string> ScrollAsync(string? refValue, string? selector, double? viewportX, double? viewportY, int clicks)
    {
        await EnsureInitializedAsync();
        Logger.Log($"[app-browser-window:{Label}] ScrollAsync: ref={refValue} selector={selector} point=({viewportX},{viewportY}) clicks={clicks}");
        double x, y;
        if (viewportX is not null && viewportY is not null)
        {
            (x, y) = (viewportX.Value, viewportY.Value);
        }
        else
        {
            var center = await GetElementCenterInViewportAsync(refValue, selector);
            if (center is null)
            {
                Logger.Log($"[app-browser-window:{Label}] ScrollAsync: no matching element");
                return "no-element";
            }
            (x, y) = (center.Value.x, center.Value.y);
        }
        var screenPoint = ViewportToScreen(x, y);
        ShowNow();
        await Task.Delay(50);
        RealInput.Scroll((int)screenPoint.X, (int)screenPoint.Y, clicks);
        Logger.Log($"[app-browser-window:{Label}] ScrollAsync: done ({clicks} click(s) at screen=({screenPoint.X:F0},{screenPoint.Y:F0}))");
        return "scrolled (real OS input)";
    }

    /// <summary>Real click to focus the element, then real SendInput keystrokes -- see
    /// ClickRealAsync's doc comment for why/when this is needed over TypeAsync.</summary>
    public async Task<string> TypeRealAsync(string? refValue, string? selector, string text)
    {
        await EnsureInitializedAsync();
        Logger.Log($"[app-browser-window:{Label}] TypeRealAsync: ref={refValue} selector={selector} text.Length={text.Length}");
        var center = await GetElementCenterInViewportAsync(refValue, selector);
        if (center is null)
        {
            Logger.Log($"[app-browser-window:{Label}] TypeRealAsync: no matching element");
            return "no-element";
        }
        var screenPoint = _webView!.PointToScreen(new System.Windows.Point(center.Value.x, center.Value.y));
        ShowNow();
        await Task.Delay(80);
        RealInput.Click((int)screenPoint.X, (int)screenPoint.Y);
        await Task.Delay(80);
        RealInput.TypeUnicode(text);
        Logger.Log($"[app-browser-window:{Label}] TypeRealAsync: done");
        return "typed (real OS input)";
    }

    /// <summary>Real SendInput keypress on whatever currently has focus in this window -- see
    /// ClickRealAsync's doc comment for why/when this is needed over PressKeyAsync.</summary>
    public async Task<string> PressKeyRealAsync(string key)
    {
        await EnsureInitializedAsync();
        Logger.Log($"[app-browser-window:{Label}] PressKeyRealAsync: key={key}");
        ShowNow();
        await Task.Delay(50);
        RealInput.PressCombo(key);
        Logger.Log($"[app-browser-window:{Label}] PressKeyRealAsync: done");
        return "pressed (real OS input)";
    }

    /// <summary>Optional crop (x/y/width/height, in the captured bitmap's own pixel space) and/or
    /// proportional maxWidth downscale -- per explicit instruction (2026-09-04): app_browser_screenshot
    /// was the one screenshot tool in the whole app with NO region/downscale support at all (unlike
    /// take_screenshot and capture_window, which both already had it), so a caller that only needed
    /// a small part of the page still paid full-page image-token cost every time. Same crop-then-
    /// downscale logic as MCP/window-screenshot's own native/Program.cs (ApplyCropAndDownscale),
    /// ported here since this capture happens in-process (WebView2's CapturePreviewAsync) rather
    /// than through that separate native exe.</summary>
    public async Task<byte[]> ScreenshotAsync(int? cropX = null, int? cropY = null, int? cropWidth = null, int? cropHeight = null, int? maxWidth = null)
    {
        await EnsureInitializedAsync();
        Logger.Log($"[app-browser-window:{Label}] ScreenshotAsync: capturing preview...");
        using var stream = new MemoryStream();
        await _webView!.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, stream);

        // No explicit maxWidth means "apply the sensible default cap", not
        // "skip downscaling entirely" -- see DefaultScreenshotMaxWidth's own
        // doc comment. A caller that genuinely wants full native resolution
        // can still ask for it via an explicit, larger maxWidth.
        var effectiveMaxWidth = maxWidth ?? DefaultScreenshotMaxWidth;

        stream.Position = 0;
        using var source = new Bitmap(stream);
        if (cropX is null && source.Width <= effectiveMaxWidth)
        {
            var bytes = stream.ToArray();
            Logger.Log($"[app-browser-window:{Label}] ScreenshotAsync: done, no crop/downscale needed ({bytes.Length} bytes, {source.Width}px wide)");
            return bytes;
        }

        using var final = ApplyCropAndDownscale(source, cropX, cropY, cropWidth, cropHeight, effectiveMaxWidth);
        using var outStream = new MemoryStream();
        final.Save(outStream, ImageFormat.Png);
        var croppedBytes = outStream.ToArray();
        Logger.Log($"[app-browser-window:{Label}] ScreenshotAsync: done, cropped/resized ({croppedBytes.Length} bytes, {final.Width}x{final.Height})");
        return croppedBytes;
    }

    private static Bitmap ApplyCropAndDownscale(Bitmap source, int? cropX, int? cropY, int? cropWidth, int? cropHeight, int? maxWidth)
    {
        var current = source;

        if (cropX is not null && cropY is not null && cropWidth is not null && cropHeight is not null)
        {
            var cropRect = Rectangle.Intersect(
                new Rectangle(cropX.Value, cropY.Value, cropWidth.Value, cropHeight.Value),
                new Rectangle(0, 0, current.Width, current.Height));
            if (cropRect.Width <= 0 || cropRect.Height <= 0)
            {
                throw new InvalidOperationException("Crop rectangle does not intersect the captured bitmap.");
            }
            var cropped = current.Clone(cropRect, current.PixelFormat);
            if (!ReferenceEquals(current, source)) current.Dispose();
            current = cropped;
        }

        if (maxWidth is not null && current.Width > maxWidth.Value)
        {
            var scale = (double)maxWidth.Value / current.Width;
            var newWidth = maxWidth.Value;
            var newHeight = Math.Max(1, (int)Math.Round(current.Height * scale));
            var resized = new Bitmap(newWidth, newHeight);
            using (var g = Graphics.FromImage(resized))
            {
                g.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.HighQualityBicubic;
                g.DrawImage(current, 0, 0, newWidth, newHeight);
            }
            if (!ReferenceEquals(current, source)) current.Dispose();
            current = resized;
        }

        return current;
    }

    public string CurrentUrl => _webView?.Source?.ToString() ?? "";

    [DllImport("user32.dll")] private static extern IntPtr WindowFromPoint(POINT p);
    [DllImport("user32.dll")] private static extern IntPtr GetAncestor(IntPtr hwnd, uint gaFlags);
    private const uint GA_ROOT = 2;
    [StructLayout(LayoutKind.Sequential)]
    private struct POINT { public int X; public int Y; }

    /// <summary>
    /// True if THIS window's own HWND is genuinely the topmost thing on
    /// screen at its own center point right now -- i.e. not obscured by
    /// another window. Confirmed live (2026-08-31) as a real gap: several
    /// labeled windows (whatsapp, telegram, chatgpt) sitting at overlapping
    /// screen positions meant a coordinate-based click could silently land
    /// in the wrong window with no way to tell beforehand. WindowFromPoint
    /// returns whatever child HWND is under the point (often the WebView2
    /// control's own child window, not this top-level Window), so this
    /// walks up to the root window via GetAncestor before comparing.
    /// </summary>
    public bool IsVisibleOnTop()
    {
        var hwnd = new WindowInteropHelper(this).Handle;
        if (hwnd == IntPtr.Zero) return false;
        var centerScreen = PointToScreen(new System.Windows.Point(ActualWidth / 2, ActualHeight / 2));
        var pointAtCenter = WindowFromPoint(new POINT { X = (int)centerScreen.X, Y = (int)centerScreen.Y });
        var rootAtCenter = GetAncestor(pointAtCenter, GA_ROOT);
        var result = rootAtCenter == hwnd;
        Logger.Log($"[app-browser-window:{Label}] IsVisibleOnTop: hwnd={hwnd} rootAtCenter={rootAtCenter} -> {result}");
        return result;
    }

    public event Action<AppBrowserWindow>? WindowClosed;

    private void OnClosing(object? sender, System.ComponentModel.CancelEventArgs e)
    {
        Logger.Log($"[app-browser-window:{Label}] OnClosing: window closing (url={CurrentUrl})");
        WindowClosed?.Invoke(this);
    }
}

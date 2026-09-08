using System.Diagnostics;
using System.IO;
using System.Net;
using System.Text;
using System.Text.Json;
using Caroline.Services;

namespace Caroline.Native;

/// <summary>
/// Local HTTP bridge for Caroline's embedded multi-window browser (see
/// AppBrowserWindow) -- the backend is a separate Node process (server.ts's
/// in-process "caroline-appbrowser" MCP tool, appBrowser.ts), so it can't
/// call directly into this WPF process's WebView2 windows; it calls this
/// tiny local HTTP server instead, same shape (and same reasoning: a local,
/// same-machine-only, unauthenticated control surface) as server.ts's own
/// /api/* that BackendHealthWatchdog polls in the opposite direction. Every
/// request that touches a window is marshaled onto the UI thread via
/// Dispatcher -- WebView2/WPF objects can only be touched from there.
///
/// Every step here is logged (entry, each dispatch branch, timing) --
/// confirmed live (2026-08-31) that a silent gap between "listening on
/// :8767" and the client-side timeout, with nothing in between, made a real
/// bug ("window never even tried to open" -- Show() was called only AFTER
/// WebView2 init finished, see AppBrowserWindow.ShowNow's doc comment)
/// impossible to diagnose from logs alone. Never again: every request, every
/// dispatch branch, and (in AppBrowserWindow itself) every WebView2
/// operation logs its own entry/exit/timing unconditionally, not just on
/// error.
/// </summary>
public sealed class AppBrowserHost : IDisposable
{
    public const int Port = 8767;
    // Each labeled window gets its own CDP debugging port (see
    // AppBrowserWindow.CdpPort) -- starts well clear of the fixed ports the
    // OTHER browser MCP profiles already use (9322-9324, 9822 -- see
    // workspace.ts's defaultServers) so the two systems can never collide.
    private const int FirstCdpPort = 9900;
    // Above OPEN_TIMEOUT_MS (90s) and the WebView2 init timeout (60s) on the
    // Node/AppBrowserWindow side respectively -- this is the outermost
    // safety net, not the primary bound; see HandleRequest's own comment.
    private static readonly TimeSpan HandleRequestTimeout = TimeSpan.FromSeconds(100);

    /// <summary>Wired by MainWindow's constructor -- lets /test_visual_mode below
    /// trigger VisualModeWindow's init directly, isolated from the chat/TTS pipeline,
    /// for diagnosing the 2026-09-03 WebView2-init hang.</summary>
    public Caroline.VisualModeManager? VisualMode { get; set; }

    private readonly HttpListener _listener = new();
    private readonly Dictionary<string, AppBrowserWindow> _windows = new(StringComparer.OrdinalIgnoreCase);
    private readonly object _windowsLock = new();
    private int _nextCdpPort = FirstCdpPort;
    private CancellationTokenSource? _cts;

    public void Start()
    {
        _listener.Prefixes.Add($"http://127.0.0.1:{Port}/");
        _listener.Start();
        _cts = new CancellationTokenSource();
        _ = RunLoop(_cts.Token);
        Logger.Log($"[app-browser-host] listening on http://127.0.0.1:{Port}/");
    }

    private async Task RunLoop(CancellationToken ct)
    {
        Logger.Log("[app-browser-host] RunLoop: entered");
        while (!ct.IsCancellationRequested)
        {
            HttpListenerContext ctx;
            try
            {
                ctx = await _listener.GetContextAsync();
            }
            catch (Exception) when (ct.IsCancellationRequested)
            {
                Logger.Log("[app-browser-host] RunLoop: GetContextAsync cancelled, exiting loop");
                return;
            }
            catch (Exception ex)
            {
                Logger.Log($"[app-browser-host] RunLoop: GetContextAsync threw: {ex}");
                continue;
            }
            Logger.Log($"[app-browser-host] RunLoop: accepted connection from {ctx.Request.RemoteEndPoint}, dispatching HandleRequest");
            _ = HandleRequest(ctx);
        }
        Logger.Log("[app-browser-host] RunLoop: loop condition false, exiting");
    }

    private async Task HandleRequest(HttpListenerContext ctx)
    {
        var sw = Stopwatch.StartNew();
        var path = ctx.Request.Url?.AbsolutePath ?? "";
        Logger.Log($"[app-browser-host] HandleRequest: entered, method={ctx.Request.HttpMethod} path={path}");
        try
        {
            string body = "{}";
            if (ctx.Request.HttpMethod == "POST")
            {
                using var reader = new StreamReader(ctx.Request.InputStream, ctx.Request.ContentEncoding);
                body = await reader.ReadToEndAsync();
                Logger.Log($"[app-browser-host] HandleRequest: body read ({body.Length} chars)");
            }
            // Hard ceiling on top of whatever bound (if any) the specific
            // Dispatch branch has itself -- confirmed live (2026-09-01) that
            // a stuck WebView2 init left this task (and, apparently, the
            // whole listener's ability to serve OTHER requests too) hanging
            // forever with no HTTP response ever sent, the underlying
            // connection stuck permanently in CLOSE_WAIT. Every request
            // handled here now unconditionally gets an answer -- a real one
            // or a clear timeout error -- within this ceiling, no exceptions.
            var dispatchTask = Dispatch(path, body);
            var (status, json) = await Task.WhenAny(dispatchTask, Task.Delay(HandleRequestTimeout)) == dispatchTask
                ? await dispatchTask
                : (504, JsonSerializer.Serialize(new { error = $"Request to {path} did not complete within {HandleRequestTimeout.TotalSeconds:F0}s." }));
            Logger.Log($"[app-browser-host] HandleRequest: Dispatch returned status={status} ({sw.Elapsed.TotalSeconds:F1}s total), writing response ({json.Length} chars)");
            ctx.Response.StatusCode = status;
            ctx.Response.ContentType = "application/json";
            var bytes = Encoding.UTF8.GetBytes(json);
            ctx.Response.ContentLength64 = bytes.Length;
            await ctx.Response.OutputStream.WriteAsync(bytes);
            Logger.Log($"[app-browser-host] HandleRequest: response written, done ({sw.Elapsed.TotalSeconds:F1}s total)");
        }
        catch (Exception ex)
        {
            Logger.Log($"[app-browser-host] HandleRequest: threw after {sw.Elapsed.TotalSeconds:F1}s: {ex}");
            try
            {
                ctx.Response.StatusCode = 500;
                var bytes = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new { error = ex.Message }));
                await ctx.Response.OutputStream.WriteAsync(bytes);
            }
            catch (Exception writeEx)
            {
                Logger.Log($"[app-browser-host] HandleRequest: also failed writing the error response: {writeEx}");
            }
        }
        finally
        {
            try { ctx.Response.OutputStream.Close(); } catch { /* already closed */ }
        }
    }

    private async Task<AppBrowserWindow> GetOrCreateWindowAsync(string label)
    {
        Logger.Log($"[app-browser-host] GetOrCreateWindowAsync({label}): entered (thread={Environment.CurrentManagedThreadId})");
        return await System.Windows.Application.Current.Dispatcher.InvokeAsync(async () =>
        {
            Logger.Log($"[app-browser-host] GetOrCreateWindowAsync({label}): now on UI thread (thread={Environment.CurrentManagedThreadId})");
            lock (_windowsLock)
            {
                if (_windows.TryGetValue(label, out var existing))
                {
                    Logger.Log($"[app-browser-host] GetOrCreateWindowAsync({label}): reusing existing window");
                    return existing;
                }
            }
            var cdpPort = _nextCdpPort++;
            Logger.Log($"[app-browser-host] GetOrCreateWindowAsync({label}): no existing window, constructing a new one (cdpPort={cdpPort})");
            var win = new AppBrowserWindow(label, cdpPort);
            win.WindowClosed += w =>
            {
                Logger.Log($"[app-browser-host] GetOrCreateWindowAsync: WindowClosed fired for {w.Label}, removing from tracking");
                lock (_windowsLock) { _windows.Remove(w.Label); }
            };
            int existingCount;
            lock (_windowsLock) { existingCount = _windows.Count; _windows[label] = win; }
            // Cascade each new window's position off the count already open --
            // confirmed live (2026-08-31) that several labeled windows
            // (whatsapp, telegram, messenger, chatgpt) otherwise all land at
            // the exact same screen position, so a coordinate-based click
            // could land in the wrong one entirely. Wraps every 10 windows
            // (WrapEvery * CascadeOffsetPx stays comfortably on-screen) --
            // moot in practice at MaxTabs-scale counts, just a safety cap.
            const int CascadeOffsetPx = 40;
            const int WrapEvery = 10;
            var step = existingCount % WrapEvery;
            win.Left = 80 + step * CascadeOffsetPx;
            win.Top = 80 + step * CascadeOffsetPx;
            // Show the window FIRST, before WebView2 initialization -- see
            // AppBrowserWindow.ShowNow's doc comment for why this order
            // matters (the previous, wrong order was the actual bug behind
            // "window never even tried to open").
            win.ShowNow();
            await win.EnsureInitializedAsync();
            Logger.Log($"[app-browser-host] GetOrCreateWindowAsync({label}): window shown and initialized");
            return win;
        }).Task.Unwrap();
    }

    private (bool found, AppBrowserWindow? win) TryGetWindow(string label)
    {
        lock (_windowsLock)
        {
            var found = _windows.TryGetValue(label, out var w);
            Logger.Log($"[app-browser-host] TryGetWindow({label}): found={found}");
            return found ? (true, w) : (false, null);
        }
    }

    private async Task<(int status, string json)> Dispatch(string path, string body)
    {
        JsonElement root = body.Length > 0 ? JsonSerializer.Deserialize<JsonElement>(body) : default;
        string Label() => root.GetProperty("label").GetString()!;
        string? StrOrNull(string prop) => root.TryGetProperty(prop, out var v) && v.ValueKind != JsonValueKind.Null ? v.GetString() : null;
        double? DoubleOrNull(string prop) => root.TryGetProperty(prop, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : null;
        int IntOr(string prop, int defaultValue) => root.TryGetProperty(prop, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetInt32() : defaultValue;
        int? IntOrNull(string prop) => root.TryGetProperty(prop, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetInt32() : null;

        Logger.Log($"[app-browser-host] Dispatch: path={path}");

        if (path == "/test_visual_mode")
        {
            // Debug-only endpoint (2026-09-03, diagnosing a VisualModeWindow WebView2-init
            // hang): opens the window and shows its static frame, completely isolated from
            // the chat turn / TTS synthesis / backend -- so a hang here can ONLY be the
            // window's own WebView2 init, nothing else in the pipeline. GET
            // http://127.0.0.1:8767/test_visual_mode from curl or a browser triggers it.
            Logger.Log("[app-browser-host] Dispatch(/test_visual_mode): entered");
            if (VisualMode == null)
            {
                Logger.Log("[app-browser-host] Dispatch(/test_visual_mode): VisualMode not wired, returning 500");
                return (500, JsonSerializer.Serialize(new { error = "VisualMode not wired" }));
            }
            var testId = "test-" + Guid.NewGuid().ToString("N")[..8];
            var sw = Stopwatch.StartNew();
            Logger.Log($"[app-browser-host] Dispatch(/test_visual_mode): calling HandleStartAsync requestId={testId} on UI thread");
            try
            {
                await ((Task)System.Windows.Application.Current.Dispatcher.Invoke(
                    () => VisualMode.HandleStartAsync(testId)));
                Logger.Log($"[app-browser-host] Dispatch(/test_visual_mode): HandleStartAsync returned normally after {sw.Elapsed.TotalSeconds:F1}s");
                return (200, JsonSerializer.Serialize(new { requestId = testId, elapsedSeconds = sw.Elapsed.TotalSeconds }));
            }
            catch (Exception ex)
            {
                Logger.Log($"[app-browser-host] Dispatch(/test_visual_mode): HandleStartAsync threw after {sw.Elapsed.TotalSeconds:F1}s: {ex}");
                return (500, JsonSerializer.Serialize(new { error = ex.ToString(), elapsedSeconds = sw.Elapsed.TotalSeconds }));
            }
        }

        if (path == "/shutdown")
        {
            // Per explicit instruction (2026-09-03): CarolineInstaller used to always
            // force-kill a running Caroline.exe from the OUTSIDE (Process.Kill) before an
            // update, which never runs Caroline's OWN cleanup code (App.xaml.cs's Dispose()
            // sequence -- AppBrowserHost.Dispose(), BackendProcess.Dispose() with its own
            // controlled entireProcessTree kill, closing every WebView2 window properly)
            // at all; an external Kill() just tears down the OS process tree, hoping it
            // catches everything. Confirmed live as a real gap: a stray WebView2 renderer
            // process could still be found holding a file open under AppDir well after
            // that external kill supposedly finished. This endpoint lets the installer ask
            // Caroline to exit HERSELF first, through the exact same graceful path the
            // tray's "Update to ..." click already uses (UpdateChecker.UpdateNowAsync's own
            // Application.Current.Shutdown()) -- Autostart.StopRunningClient tries this
            // first now, falling back to the external hard-kill only if it doesn't work.
            Logger.Log("[app-browser-host] Dispatch(/shutdown): requested -- scheduling graceful Application.Shutdown()");
            _ = Task.Run(async () =>
            {
                // Give the HTTP response below a moment to actually reach the caller before
                // this process starts tearing itself down -- Shutdown() closing the WebView2
                // window that's hosting this very listener's response pipeline could
                // otherwise race the write.
                await Task.Delay(200);
                System.Windows.Application.Current.Dispatcher.Invoke(() => System.Windows.Application.Current.Shutdown());
            });
            return (200, JsonSerializer.Serialize(new { ok = true }));
        }

        if (path == "/process_list")
        {
            // Per explicit instruction (2026-09-06): the Node backend's own process-tree
            // bookkeeping (processReaper.ts, server.ts's descendant-process diagnostics)
            // used to shell out to PowerShell/WMI for this -- slow (300-500ms per call,
            // on every single query() creation) and, confirmed live the same day, fragile
            // enough to silently break on a quoting bug. This is the direct WinAPI
            // equivalent (kernel32.dll's Toolhelp32Snapshot, see ProcessTreeHelper) served
            // over the same local HTTP bridge Node already talks to for everything else --
            // no process spawned for the call, just a socket request to this already-
            // running one.
            var procs = ProcessTreeHelper.ListAll();
            Logger.Log($"[app-browser-host] Dispatch(/process_list): {procs.Count} process(es)");
            return (200, JsonSerializer.Serialize(procs.Select(p => new { pid = p.Pid, parentPid = p.ParentPid, name = p.Name })));
        }

        if (path == "/kill_process")
        {
            // Process.Kill() is itself a thin wrapper over WinAPI's TerminateProcess --
            // no external process spawned (unlike the taskkill.exe call this replaces).
            var pid = root.GetProperty("pid").GetInt32();
            Logger.Log($"[app-browser-host] Dispatch(/kill_process): pid={pid}");
            try
            {
                using var proc = Process.GetProcessById(pid);
                proc.Kill(entireProcessTree: true);
                Logger.Log($"[app-browser-host] Dispatch(/kill_process): pid={pid} was alive, killed its whole tree");
                return (200, JsonSerializer.Serialize(new { ok = true, wasAlive = true }));
            }
            catch (ArgumentException)
            {
                // Process.GetProcessById throws this for a pid that no longer exists --
                // not an error for a caller whose whole point is "kill it IF it's still there".
                Logger.Log($"[app-browser-host] Dispatch(/kill_process): pid={pid} already gone");
                return (200, JsonSerializer.Serialize(new { ok = true, wasAlive = false }));
            }
            catch (Exception ex)
            {
                Logger.Log($"[app-browser-host] Dispatch(/kill_process): pid={pid} failed: {ex.Message}");
                return (500, JsonSerializer.Serialize(new { ok = false, error = ex.Message }));
            }
        }

        if (path == "/list" || path == "/list/")
        {
            var list = await System.Windows.Application.Current.Dispatcher.InvokeAsync(() =>
            {
                lock (_windowsLock)
                {
                    return _windows.Select(kv => new { label = kv.Key, url = kv.Value.CurrentUrl, cdpPort = kv.Value.CdpPort }).ToArray();
                }
            });
            Logger.Log($"[app-browser-host] Dispatch(/list): {list.Length} window(s) open");
            return (200, JsonSerializer.Serialize(list));
        }

        if (path == "/fill_file_dialog")
        {
            // Not tied to any particular labeled window -- a native OS file
            // picker is its own top-level window, triggered by whichever
            // window (an app_browser tab, or anything else) opened it.
            var paths = root.GetProperty("paths").EnumerateArray().Select(p => p.GetString()!).ToArray();
            var timeoutMs = IntOr("timeoutMs", 10_000);
            Logger.Log($"[app-browser-host] Dispatch(/fill_file_dialog): paths=[{string.Join(", ", paths)}] timeoutMs={timeoutMs}");
            var result = await FileDialogHelper.FillAndConfirmAsync(paths, timeoutMs);
            Logger.Log($"[app-browser-host] Dispatch(/fill_file_dialog): result={result}");
            return (200, JsonSerializer.Serialize(new { result }));
        }

        if (path == "/open")
        {
            var label = Label();
            var url = StrOrNull("url");
            Logger.Log($"[app-browser-host] Dispatch(/open): label={label} url={url}");
            var win = await GetOrCreateWindowAsync(label);
            // Non-blocking: starts the navigation but does NOT wait for the
            // page to fully finish loading before returning -- see
            // AppBrowserWindow.StartNavigateAsync's doc comment. The window
            // is already visible by this point (GetOrCreateWindowAsync shows
            // it before WebView2 init even completes); open_app_browser
            // should return as soon as the window exists and navigation has
            // started, not block on a heavy SPA's full load.
            if (!string.IsNullOrEmpty(url)) await win.StartNavigateAsync(url);
            Logger.Log($"[app-browser-host] Dispatch(/open): done, returning (window is visible, navigation may still be in flight, cdpPort={win.CdpPort})");
            return (200, JsonSerializer.Serialize(new { ok = true, url = win.CurrentUrl, cdpPort = win.CdpPort }));
        }

        if (path == "/get_port")
        {
            // Lightweight lookup for the Node side's CDP client cache -- avoids
            // re-running /open's full get-or-create dance (which also touches
            // navigation) just to learn a port it may already have cached from
            // last time but wants to confirm is still this window's real one.
            var (found, existingWin) = TryGetWindow(Label());
            if (!found) return (404, JsonSerializer.Serialize(new { error = $"No window for label \"{Label()}\"" }));
            return (200, JsonSerializer.Serialize(new { cdpPort = existingWin!.CdpPort }));
        }

        if (path == "/close")
        {
            var label = Label();
            var (found, win) = TryGetWindow(label);
            if (found)
            {
                Logger.Log($"[app-browser-host] Dispatch(/close): closing window {label}");
                await System.Windows.Application.Current.Dispatcher.InvokeAsync(() => win!.Close());
            }
            else
            {
                Logger.Log($"[app-browser-host] Dispatch(/close): no window {label} to close");
            }
            return (200, JsonSerializer.Serialize(new { ok = true }));
        }

        // Every remaining op needs an existing (or freshly-opened) window.
        var target = await GetOrCreateWindowAsync(Label());

        switch (path)
        {
            case "/navigate":
                await target.NavigateAsync(root.GetProperty("url").GetString()!);
                Logger.Log($"[app-browser-host] Dispatch(/navigate): done, url={target.CurrentUrl}");
                return (200, JsonSerializer.Serialize(new { ok = true, url = target.CurrentUrl }));

            case "/is_visible_on_top":
            {
                var onTop = await System.Windows.Application.Current.Dispatcher.InvokeAsync(target.IsVisibleOnTop);
                Logger.Log($"[app-browser-host] Dispatch(/is_visible_on_top): onTop={onTop}");
                return (200, JsonSerializer.Serialize(new { onTop }));
            }

            // snapshot/find/evaluate and the non-real (JS-dispatch) paths of
            // click/type/press_key moved to the Node side (appBrowserCdp.ts),
            // which connects directly over this window's own CDP port
            // (see CdpPort/AppBrowserWindow) instead of routing through this
            // HTTP bridge -- see AppBrowserWindow.xaml.cs's own comment on
            // why. This host now only ever handles: window lifecycle,
            // screenshots, and real (OS-level SendInput) click/type/
            // press_key/scroll, none of which are page-content operations.

            case "/click":
            {
                var x = DoubleOrNull("x");
                var y = DoubleOrNull("y");
                if (x is not null && y is not null)
                {
                    // Raw viewport coordinates -- always a real OS click (no
                    // DOM element to JS-dispatch against in the first place).
                    var coordResult = await target.ClickAtPointAsync(x.Value, y.Value);
                    Logger.Log($"[app-browser-host] Dispatch(/click): coord result={coordResult}");
                    return (200, JsonSerializer.Serialize(new { result = coordResult }));
                }
                var result = await target.ClickRealAsync(StrOrNull("ref"), StrOrNull("selector"));
                Logger.Log($"[app-browser-host] Dispatch(/click): result={result}");
                return (200, JsonSerializer.Serialize(new { result }));
            }

            case "/scroll":
            {
                var result = await target.ScrollAsync(StrOrNull("ref"), StrOrNull("selector"), DoubleOrNull("x"), DoubleOrNull("y"), IntOr("clicks", -3));
                Logger.Log($"[app-browser-host] Dispatch(/scroll): result={result}");
                return (200, JsonSerializer.Serialize(new { result }));
            }

            case "/type":
            {
                var text = root.GetProperty("text").GetString()!;
                var result = await target.TypeRealAsync(StrOrNull("ref"), StrOrNull("selector"), text);
                Logger.Log($"[app-browser-host] Dispatch(/type): textLen={text.Length} result={result}");
                return (200, JsonSerializer.Serialize(new { result }));
            }

            case "/press_key":
            {
                var key = root.GetProperty("key").GetString()!;
                var result = await target.PressKeyRealAsync(key);
                Logger.Log($"[app-browser-host] Dispatch(/press_key): key={key} result={result}");
                return (200, JsonSerializer.Serialize(new { result }));
            }

            case "/screenshot":
            {
                var png = await target.ScreenshotAsync(IntOrNull("x"), IntOrNull("y"), IntOrNull("width"), IntOrNull("height"), IntOrNull("maxWidth"));
                Logger.Log($"[app-browser-host] Dispatch(/screenshot): {png.Length} byte(s) captured");
                return (200, JsonSerializer.Serialize(new { imageBase64 = Convert.ToBase64String(png) }));
            }

            default:
                Logger.Log($"[app-browser-host] Dispatch: unknown op {path}");
                return (404, JsonSerializer.Serialize(new { error = $"Unknown op: {path}" }));
        }
    }

    public void Dispose()
    {
        Logger.Log("[app-browser-host] Dispose: entered");
        _cts?.Cancel();
        try { _listener.Stop(); } catch { /* already stopped */ }
        try { _listener.Close(); } catch { /* already closed */ }
        Logger.Log("[app-browser-host] Dispose: done");
    }
}

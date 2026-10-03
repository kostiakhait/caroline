using System.Diagnostics;
using System.IO;
using System.Net;
using System.Text;
using System.Text.Json;
using Caroline.NativeHost.Native;
using Caroline.NativeHost.Services;

namespace Caroline.NativeHost;

/// <summary>
/// Local HTTP bridge for Caroline's embedded multi-window browser (see
/// AppBrowserWindow) -- moved out of Caroline.exe itself (2026-10-03, "Extract
/// Caroline's embedded browser into a separate process") into this small,
/// standalone WPF process so a stuck WebView2 profile or a runaway Chromium
/// renderer can no longer threaten Caroline's own UI process. backend-py's
/// app_browser_plugin.py talks to this exact same port (8767, unchanged) and
/// lazily launches this .exe if it isn't already running -- see that file's
/// own doc comment for the launch side of this split.
///
/// This class is a trimmed copy of the original Caroline.Native.AppBrowserHost:
/// it keeps every browser-lifecycle/real-input endpoint plus /process_list and
/// /kill_process (plain WinAPI process-tree utilities with zero WPF/UI-thread
/// dependency -- they rode this same already-open local socket purely for
/// convenience, nothing to do with browser windows specifically, and porting
/// ProcessTreeHelper to Python for no benefit wasn't worth it; see the plan
/// doc). /test_visual_mode and the ORIGINAL /shutdown (which closed Caroline's
/// own main window) stayed behind in Caroline.exe's own much smaller
/// AppControlHost (now on port 8768) -- they're about Caroline's avatar/TTS
/// pipeline and Caroline's own process respectively, not about this one.
/// This class's own /shutdown below closes every AppBrowserWindow this
/// process owns, then exits itself -- used by CarolineInstaller's
/// Autostart.cs before an update, same reasoning as Caroline.exe's /shutdown
/// always had (a graceful close releases WebView2's profile-directory locks;
/// an external hard-kill can leave a stray renderer process still holding one).
///
/// Every request that touches a window is marshaled onto the UI thread via
/// Dispatcher -- WebView2/WPF objects can only be touched from there.
/// </summary>
public sealed class AppBrowserHost : IDisposable
{
    public const int Port = 8767;
    // Each labeled window gets its own CDP debugging port (see
    // AppBrowserWindow.CdpPort) -- starts well clear of the fixed ports the
    // OTHER browser MCP profiles already use (9322-9324, 9822 -- see
    // workspace.ts's defaultServers) so the two systems can never collide.
    private const int FirstCdpPort = 9900;
    // Above OPEN_TIMEOUT_S (90s, app_browser_plugin.py) and the WebView2 init
    // timeout (60s) on the AppBrowserWindow side respectively -- this is the
    // outermost safety net, not the primary bound; see HandleRequest's own
    // comment.
    private static readonly TimeSpan HandleRequestTimeout = TimeSpan.FromSeconds(100);

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
            // Dispatch branch has itself -- a stuck WebView2 init must never
            // leave this request (or the whole listener's ability to serve
            // OTHER requests) hanging forever with no HTTP response ever
            // sent. Every request handled here now unconditionally gets an
            // answer -- a real one or a clear timeout error -- within this
            // ceiling, no exceptions.
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
            // several labeled windows otherwise all land at the exact same
            // screen position, so a coordinate-based click could land in the
            // wrong one entirely. Wraps every 10 windows (WrapEvery *
            // CascadeOffsetPx stays comfortably on-screen) -- moot in
            // practice at MaxTabs-scale counts, just a safety cap.
            const int CascadeOffsetPx = 40;
            const int WrapEvery = 10;
            var step = existingCount % WrapEvery;
            win.Left = 80 + step * CascadeOffsetPx;
            win.Top = 80 + step * CascadeOffsetPx;
            // Show the window FIRST, before WebView2 initialization -- see
            // AppBrowserWindow.ShowNow's doc comment for why this order
            // matters.
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

        if (path == "/shutdown")
        {
            // Asked by CarolineInstaller's Autostart.cs before an update, same
            // reasoning Caroline.exe's own /shutdown always had: close every
            // WebView2 window through its OWN OnClosing path (releases its
            // profile-directory lock properly) before this process exits,
            // rather than relying on an external hard-kill to catch
            // everything. Give the HTTP response a moment to actually reach
            // the caller first.
            Logger.Log("[app-browser-host] Dispatch(/shutdown): requested -- closing all windows then exiting");
            _ = Task.Run(async () =>
            {
                await Task.Delay(200);
                await System.Windows.Application.Current.Dispatcher.InvokeAsync(() =>
                {
                    AppBrowserWindow[] toClose;
                    lock (_windowsLock) { toClose = _windows.Values.ToArray(); }
                    foreach (var w in toClose)
                    {
                        try { w.Close(); }
                        catch (Exception ex) { Logger.Log($"[app-browser-host] Dispatch(/shutdown): closing {w.Label} threw (ignored): {ex.Message}"); }
                    }
                    System.Windows.Application.Current.Shutdown();
                });
            });
            return (200, JsonSerializer.Serialize(new { ok = true }));
        }

        if (path == "/process_list")
        {
            // Plain WinAPI process-tree enumeration (kernel32.dll's
            // Toolhelp32Snapshot, see ProcessTreeHelper) served over this
            // already-running local socket -- nothing to do with browser
            // windows specifically, see this class's own doc comment for why
            // it lives here rather than being ported to Python.
            var procs = ProcessTreeHelper.ListAll();
            Logger.Log($"[app-browser-host] Dispatch(/process_list): {procs.Count} process(es)");
            return (200, JsonSerializer.Serialize(procs.Select(p => new { pid = p.Pid, parentPid = p.ParentPid, name = p.Name })));
        }

        if (path == "/kill_process")
        {
            // Process.Kill() is itself a thin wrapper over WinAPI's
            // TerminateProcess -- no external process spawned.
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
            // Lightweight lookup for backend-py's CDP client cache -- avoids
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
            // click/type/press_key are handled by backend-py's own
            // app_browser_cdp.py, which connects directly over this window's
            // own CDP port (see CdpPort/AppBrowserWindow) instead of routing
            // through this HTTP bridge. This host only ever handles: window
            // lifecycle, screenshots, and real (OS-level SendInput)
            // click/type/press_key/scroll, none of which are page-content
            // operations.

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

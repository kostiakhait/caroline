using System.Diagnostics;
using System.IO;
using System.Net;
using System.Text;
using System.Text.Json;
using Caroline.Services;

namespace Caroline.Native;

/// <summary>
/// Local HTTP control surface for Caroline.exe itself -- what's left of the
/// old AppBrowserHost.cs after its browser-window endpoints moved to the new
/// standalone Caroline.NativeHost process (2026-10-03, "Extract Caroline's
/// embedded browser into a separate process"). Only two things remain, and
/// both are genuinely about THIS process, not about browser windows:
///
/// - /shutdown: asks Caroline to exit herself gracefully (CarolineInstaller's
///   Autostart.cs calls this before an update, through the exact same
///   graceful path the tray's "Update to ..." click already uses --
///   UpdateChecker.UpdateNowAsync's own Application.Current.Shutdown()).
/// - /test_visual_mode: a 2026-09-03 debug-only hook into VisualModeManager
///   (Caroline's avatar/TTS visual-mode window), isolated from the chat/TTS
///   pipeline for diagnosing a WebView2-init hang.
///
/// Moved from port 8767 (now Caroline.NativeHost's own) to 8768 -- see
/// CarolineInstaller/Autostart.cs's own AppControlHostPort literal, which
/// needed updating to match.
/// </summary>
public sealed class AppControlHost : IDisposable
{
    public const int Port = 8768;
    private static readonly TimeSpan HandleRequestTimeout = TimeSpan.FromSeconds(100);

    /// <summary>Wired by MainWindow's constructor -- lets /test_visual_mode below
    /// trigger VisualModeWindow's init directly, isolated from the chat/TTS pipeline,
    /// for diagnosing the 2026-09-03 WebView2-init hang.</summary>
    public Caroline.VisualModeManager? VisualMode { get; set; }

    private readonly HttpListener _listener = new();
    private CancellationTokenSource? _cts;

    public void Start()
    {
        _listener.Prefixes.Add($"http://127.0.0.1:{Port}/");
        _listener.Start();
        _cts = new CancellationTokenSource();
        _ = RunLoop(_cts.Token);
        Logger.Log($"[app-control-host] listening on http://127.0.0.1:{Port}/");
    }

    private async Task RunLoop(CancellationToken ct)
    {
        Logger.Log("[app-control-host] RunLoop: entered");
        while (!ct.IsCancellationRequested)
        {
            HttpListenerContext ctx;
            try
            {
                ctx = await _listener.GetContextAsync();
            }
            catch (Exception) when (ct.IsCancellationRequested)
            {
                Logger.Log("[app-control-host] RunLoop: GetContextAsync cancelled, exiting loop");
                return;
            }
            catch (Exception ex)
            {
                Logger.Log($"[app-control-host] RunLoop: GetContextAsync threw: {ex}");
                continue;
            }
            Logger.Log($"[app-control-host] RunLoop: accepted connection from {ctx.Request.RemoteEndPoint}, dispatching HandleRequest");
            _ = HandleRequest(ctx);
        }
        Logger.Log("[app-control-host] RunLoop: loop condition false, exiting");
    }

    private async Task HandleRequest(HttpListenerContext ctx)
    {
        var sw = Stopwatch.StartNew();
        var path = ctx.Request.Url?.AbsolutePath ?? "";
        Logger.Log($"[app-control-host] HandleRequest: entered, method={ctx.Request.HttpMethod} path={path}");
        try
        {
            var dispatchTask = Dispatch(path);
            var (status, json) = await Task.WhenAny(dispatchTask, Task.Delay(HandleRequestTimeout)) == dispatchTask
                ? await dispatchTask
                : (504, JsonSerializer.Serialize(new { error = $"Request to {path} did not complete within {HandleRequestTimeout.TotalSeconds:F0}s." }));
            Logger.Log($"[app-control-host] HandleRequest: Dispatch returned status={status} ({sw.Elapsed.TotalSeconds:F1}s total), writing response ({json.Length} chars)");
            ctx.Response.StatusCode = status;
            ctx.Response.ContentType = "application/json";
            var bytes = Encoding.UTF8.GetBytes(json);
            ctx.Response.ContentLength64 = bytes.Length;
            await ctx.Response.OutputStream.WriteAsync(bytes);
            Logger.Log($"[app-control-host] HandleRequest: response written, done ({sw.Elapsed.TotalSeconds:F1}s total)");
        }
        catch (Exception ex)
        {
            Logger.Log($"[app-control-host] HandleRequest: threw after {sw.Elapsed.TotalSeconds:F1}s: {ex}");
            try
            {
                ctx.Response.StatusCode = 500;
                var bytes = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new { error = ex.Message }));
                await ctx.Response.OutputStream.WriteAsync(bytes);
            }
            catch (Exception writeEx)
            {
                Logger.Log($"[app-control-host] HandleRequest: also failed writing the error response: {writeEx}");
            }
        }
        finally
        {
            try { ctx.Response.OutputStream.Close(); } catch { /* already closed */ }
        }
    }

    private async Task<(int status, string json)> Dispatch(string path)
    {
        Logger.Log($"[app-control-host] Dispatch: path={path}");

        if (path == "/test_visual_mode")
        {
            // Debug-only endpoint (2026-09-03, diagnosing a VisualModeWindow WebView2-init
            // hang): opens the window and shows its static frame, completely isolated from
            // the chat turn / TTS synthesis / backend -- so a hang here can ONLY be the
            // window's own WebView2 init, nothing else in the pipeline. GET
            // http://127.0.0.1:8768/test_visual_mode from curl or a browser triggers it.
            Logger.Log("[app-control-host] Dispatch(/test_visual_mode): entered");
            if (VisualMode == null)
            {
                Logger.Log("[app-control-host] Dispatch(/test_visual_mode): VisualMode not wired, returning 500");
                return (500, JsonSerializer.Serialize(new { error = "VisualMode not wired" }));
            }
            var testId = "test-" + Guid.NewGuid().ToString("N")[..8];
            var sw = Stopwatch.StartNew();
            Logger.Log($"[app-control-host] Dispatch(/test_visual_mode): calling HandleStartAsync requestId={testId} on UI thread");
            try
            {
                await ((Task)System.Windows.Application.Current.Dispatcher.Invoke(
                    () => VisualMode.HandleStartAsync(testId)));
                Logger.Log($"[app-control-host] Dispatch(/test_visual_mode): HandleStartAsync returned normally after {sw.Elapsed.TotalSeconds:F1}s");
                return (200, JsonSerializer.Serialize(new { requestId = testId, elapsedSeconds = sw.Elapsed.TotalSeconds }));
            }
            catch (Exception ex)
            {
                Logger.Log($"[app-control-host] Dispatch(/test_visual_mode): HandleStartAsync threw after {sw.Elapsed.TotalSeconds:F1}s: {ex}");
                return (500, JsonSerializer.Serialize(new { error = ex.ToString(), elapsedSeconds = sw.Elapsed.TotalSeconds }));
            }
        }

        if (path == "/shutdown")
        {
            // Per explicit instruction (2026-09-03): CarolineInstaller used to always
            // force-kill a running Caroline.exe from the OUTSIDE (Process.Kill) before an
            // update, which never runs Caroline's OWN cleanup code (App.xaml.cs's Dispose()
            // sequence -- SupervisorClient.Dispose(), see its own 2026-09-27 doc comment) at
            // all; an external Kill() just tears down the OS process tree, hoping it catches
            // everything. This endpoint lets the installer ask Caroline to exit HERSELF
            // first, through the exact same graceful path the tray's "Update to ..." click
            // already uses (UpdateChecker.UpdateNowAsync's own Application.Current.Shutdown())
            // -- Autostart.StopRunningClient tries this first now, falling back to the
            // external hard-kill only if it doesn't work.
            Logger.Log("[app-control-host] Dispatch(/shutdown): requested -- scheduling graceful Application.Shutdown()");
            _ = Task.Run(async () =>
            {
                // Give the HTTP response below a moment to actually reach the caller before
                // this process starts tearing itself down.
                await Task.Delay(200);
                System.Windows.Application.Current.Dispatcher.Invoke(() => System.Windows.Application.Current.Shutdown());
            });
            return (200, JsonSerializer.Serialize(new { ok = true }));
        }

        Logger.Log($"[app-control-host] Dispatch: unknown op {path}");
        return (404, JsonSerializer.Serialize(new { error = $"Unknown op: {path}" }));
    }

    public void Dispose()
    {
        Logger.Log("[app-control-host] Dispose: entered");
        _cts?.Cancel();
        try { _listener.Stop(); } catch { /* already stopped */ }
        try { _listener.Close(); } catch { /* already closed */ }
        Logger.Log("[app-control-host] Dispose: done");
    }
}

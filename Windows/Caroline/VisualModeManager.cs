using System;
using System.Diagnostics;
using System.IO;
using System.Threading.Tasks;
using Caroline.Services;

namespace Caroline;

/// <summary>
/// Owns Visual Mode end-to-end: a warmed XcfaRenderer PreparedModel (see
/// Configure -- resolved by the backend once at Caroline's own startup,
/// persona + day-parity, per explicit instruction), and the single
/// VisualModeWindow instance shown for the currently-playing reply.
///
/// Deliberately has NO queue of its own: chat.js's existing speechQueue
/// already serializes voice replies one at a time (playOneSpeech awaits
/// playAudioViaVisualMode before the next queued item starts) and a second
/// request is per explicit instruction supposed to WAIT, not interrupt --
/// that's exactly what already happens for free by JS not calling
/// HandleAudioAsync again until the previous call's Task completes. This
/// class only ever has at most one active window/request at a time.
/// </summary>
public sealed class VisualModeManager
{
    private readonly string _tempDir;
    private XcfaRenderer.Renderer.PreparedModel? _prepared;
    private byte[]? _staticPng;
    private readonly object _prepGate = new();

    private VisualModeWindow? _activeWindow;
    private string? _activeRequestId;

    /// <summary>Best-effort delete of a per-request temp file (audio/rendered video) --
    /// failure just leaves one stray file behind in _tempDir, never fatal to the caller,
    /// but per explicit instruction (2026-09-06) that must still be visible in the log
    /// rather than silently accumulating leaked temp files with no trace.</summary>
    private static void SafeDeleteTemp(string path)
    {
        try { File.Delete(path); }
        catch (Exception ex) { Logger.Log($"VisualModeManager: failed to delete temp file {path} (leaving it behind): {ex.Message}"); }
    }

    /// <summary>
    /// CarolineInstaller's FfmpegInstaller.cs bundles a static ffmpeg.exe under
    /// <c>&lt;Root&gt;\runtime\ffmpeg\ffmpeg.exe</c> (a sibling of AppDir, mirroring
    /// BackendProcess.cs's own CAROLINE_MODELS_DIR reasoning) so Visual Mode doesn't
    /// depend on a bare "ffmpeg" PATH lookup that only works by accident on a machine
    /// that already happens to have it installed. Falls back to the bare name (relying
    /// on PATH, the previous behavior) when the bundled copy isn't there yet -- e.g. an
    /// install from before this was added, or straight from this source tree in dev.
    /// </summary>
    private static readonly string FfmpegPath = ResolveFfmpegPath();

    private static string ResolveFfmpegPath()
    {
        var bundled = Path.Combine(AppContext.BaseDirectory, "..", "runtime", "ffmpeg", "ffmpeg.exe");
        if (File.Exists(bundled))
        {
            Logger.Log($"VisualModeManager: using bundled ffmpeg at {bundled}");
            return bundled;
        }
        Logger.Log("VisualModeManager: bundled ffmpeg not found, falling back to PATH lookup for \"ffmpeg\"");
        return "ffmpeg";
    }

    public VisualModeManager()
    {
        _tempDir = Path.Combine(Path.GetTempPath(), "CarolineVisualMode");
        Directory.CreateDirectory(_tempDir);
        // ffmpeg now runs with CreateNoWindow=true (see XcfaRenderer.Encoder.Open) -- without
        // this hook, its stderr (encoder errors, codec issues, etc.) would just vanish
        // instead of showing in an unwanted console window. Set once for the process's
        // lifetime since Encoder.LogSink is static (shared across every render).
        XcfaRenderer.Encoder.LogSink = text => Logger.Log($"[ffmpeg] {text}");
    }

    /// <summary>
    /// modelPath is what the backend resolved (persona + day-parity) at Caroline's
    /// own startup, or null when Visual Mode is disabled/unavailable (see
    /// visualMode.ts's resolveVisualModel) -- warming happens once, in the
    /// background; nothing here blocks the caller.
    /// </summary>
    public void Configure(string? modelPath)
    {
        if (string.IsNullOrEmpty(modelPath))
        {
            Logger.Log("VisualModeManager: no model to warm (Visual Mode disabled or unavailable for this profile/day).");
            return;
        }
        Task.Run(() => WarmUp(modelPath));
    }

    private void WarmUp(string modelPath)
    {
        try
        {
            Logger.Log($"VisualModeManager: warming model {modelPath} ...");
            var sw = Stopwatch.StartNew();
            // Height=480: small enough to render quickly, plenty for a corner-of-screen
            // window. RemoveBg=true is required for the transparent background this whole
            // feature is built around; representative output path only needs the .webm
            // extension (drives the alpha-channel decision -- see Renderer.Prepare's doc).
            var prepared = XcfaRenderer.Renderer.Prepare(modelPath, "preview.webm",
                new XcfaRenderer.RenderOptions { RemoveBg = true, Height = 480, Ffmpeg = FfmpegPath });
            var staticPng = prepared.GetStaticFramePng();
            lock (_prepGate)
            {
                _prepared = prepared;
                _staticPng = staticPng;
            }
            Logger.Log($"VisualModeManager: warm-up done in {sw.Elapsed.TotalSeconds:F1}s");
        }
        catch (Exception ex)
        {
            Logger.Log($"VisualModeManager: warm-up failed: {ex}");
        }
    }

    /// <summary>Fires the moment TTS generation starts. Per explicit instruction (2026-09-03,
    /// twice over): first that the static "silence" frame wasn't worth showing, then that even
    /// warming up an invisible window this early was still "too early" -- the window must not
    /// exist at all until right before actual playback (see HandleAudioAsync). This is now just
    /// bookkeeping: remember which request is "current" so a Cancel/Stop that arrives before any
    /// window exists still correctly suppresses the window HandleAudioAsync would otherwise
    /// create later for it.</summary>
    public Task HandleStartAsync(string requestId)
    {
        Logger.Log($"VisualModeManager: HandleStartAsync requestId={requestId} (no window yet -- created right before playback in HandleAudioAsync)");
        _activeRequestId = requestId;
        return Task.CompletedTask;
    }

    /// <summary>
    /// TTS audio is ready -- renders and plays it. Resolves once playback (and the 1s
    /// post-roll) is fully done. Returns whether visual playback actually happened --
    /// false means the caller (MainWindow) should tell the client to fall back to plain
    /// audio instead, so a not-yet-warmed model or a render failure degrades to normal
    /// TTS playback instead of silence. Confirmed live (2026-09-03) as a real gap: right
    /// after a fresh restart, the model can still be mid-warm-up when the first voice
    /// reply comes in, and this used to just close the window with nothing audible at
    /// all -- worse than not having Visual Mode.
    /// </summary>
    public async Task<bool> HandleAudioAsync(string requestId, byte[] audioBytes)
    {
        Logger.Log($"VisualModeManager: HandleAudioAsync requestId={requestId} bytes={audioBytes.Length}");
        if (_activeRequestId != requestId)
        {
            // Already stopped/cancelled for this request -- not a failure, and definitely
            // not something a plain-audio fallback should un-silence after the fact.
            Logger.Log("VisualModeManager: HandleAudioAsync -- requestId no longer current, ignoring (no fallback).");
            return true;
        }

        XcfaRenderer.Renderer.PreparedModel? prepared;
        lock (_prepGate) prepared = _prepared;
        if (prepared == null)
        {
            Logger.Log("VisualModeManager: HandleAudioAsync -- model not warmed (yet), signaling fallback.");
            return false;
        }

        var audioPath = Path.Combine(_tempDir, $"{requestId}.mp3");
        var outputPath = Path.Combine(_tempDir, $"{requestId}.webm");
        await File.WriteAllBytesAsync(audioPath, audioBytes);

        Logger.Log($"VisualModeManager: HandleAudioAsync requestId={requestId} render starting");
        var renderSw = Stopwatch.StartNew();
        try
        {
            await Task.Run(() => prepared.Render(audioPath, outputPath,
                new XcfaRenderer.RenderOptions { RemoveBg = true, Height = 480, Ffmpeg = FfmpegPath }));
        }
        catch (Exception ex)
        {
            Logger.Log($"VisualModeManager: HandleAudioAsync requestId={requestId} render failed: {ex} -- signaling fallback.");
            SafeDeleteTemp(audioPath);
            return false;
        }
        Logger.Log($"VisualModeManager: HandleAudioAsync requestId={requestId} render finished in {renderSw.Elapsed.TotalSeconds:F1}s");

        if (_activeRequestId != requestId)
        {
            // Stopped/cancelled while rendering -- an intentional interruption, not a
            // failure, so no fallback either (nothing left to play it into).
            Logger.Log($"VisualModeManager: HandleAudioAsync requestId={requestId} no longer current after render, skipping playback (no fallback)");
            SafeDeleteTemp(audioPath);
            SafeDeleteTemp(outputPath);
            return true;
        }

        // The window is created HERE, right before actual playback -- per explicit
        // instruction (2026-09-03, the SECOND correction on this point): even an
        // invisible, pre-warmed window sitting around since TTS-generation-start was
        // "too early." Show() still has to happen before InitializeAsync (see that
        // method's own doc comment for why -- WebView2 needs a real HWND to attach
        // to), so there's an unavoidable ~0.3-0.5s pause here while the window comes
        // up, but that's the earliest point that's actually correct to show it at.
        var window = new VisualModeWindow();
        _activeWindow = window;
        window.Show();
        window.SetVisuallyHidden(true); // hidden for the brief Init/navigate window, revealed just below once actually playing
        Logger.Log($"VisualModeManager: HandleAudioAsync requestId={requestId} window created and shown, calling InitializeAsync");
        await window.InitializeAsync();

        if (_activeRequestId != requestId)
        {
            Logger.Log($"VisualModeManager: HandleAudioAsync requestId={requestId} no longer current after InitializeAsync, closing without playing (no fallback)");
            await CloseActiveWindowAsync();
            SafeDeleteTemp(audioPath);
            SafeDeleteTemp(outputPath);
            return true;
        }
        if (!window.IsReady)
        {
            Logger.Log("VisualModeManager: HandleAudioAsync -- window failed to become ready (InitializeAsync timed out), closing, signaling fallback.");
            await CloseActiveWindowAsync();
            SafeDeleteTemp(audioPath);
            SafeDeleteTemp(outputPath);
            return false;
        }

        var stoppedEarly = false;
        var played = false;
        var playbackEnded = new TaskCompletionSource();
        void OnEnded() => playbackEnded.TrySetResult();
        window.PlaybackEnded += OnEnded;

        try
        {
            Logger.Log($"VisualModeManager: HandleAudioAsync requestId={requestId} playback starting");
            window.SetVisuallyHidden(false);
            await window.PlayVideoAsync(outputPath);
            await playbackEnded.Task;
            stoppedEarly = _activeWindow != window;
            played = true;
            Logger.Log($"VisualModeManager: HandleAudioAsync requestId={requestId} playback ended ({(stoppedEarly ? "via stop" : "naturally")})");
        }
        catch (Exception ex)
        {
            Logger.Log($"VisualModeManager: render/playback failed: {ex} -- signaling fallback.");
        }
        finally
        {
            window.PlaybackEnded -= OnEnded;
            SafeDeleteTemp(audioPath);
            SafeDeleteTemp(outputPath);
        }

        // "через 1 секунду после окончания анимации исчезать" -- 1s after playback ends,
        // THEN close, whether it ended naturally or via the Stop button.
        await Task.Delay(1000);
        if (_activeWindow == window) await CloseActiveWindowAsync();
        return played;
    }

    /// <summary>TTS synthesis itself failed -- suppress whatever HandleAudioAsync would otherwise
    /// have done for this request (no window exists yet at this point -- see HandleStartAsync).</summary>
    public async Task HandleCancelAsync(string requestId)
    {
        Logger.Log($"VisualModeManager: HandleCancelAsync requestId={requestId} matchesActive={_activeRequestId == requestId}");
        if (_activeRequestId == requestId) await CloseActiveWindowAsync();
    }

    /// <summary>
    /// User-requested stop (this window's own button, or the main chat window's
    /// Stop / queue-clear) -- closes immediately, unlike the natural end-of-
    /// playback path's 1s post-roll. TriggerStop() first so a concurrently
    /// in-flight HandleAudioAsync (awaiting playbackEnded) unblocks and runs its
    /// own cleanup instead of hanging forever; the immediate CloseActiveWindowAsync
    /// right after is what actually makes the window disappear right away --
    /// HandleAudioAsync's own tail-end close is then a no-op (_activeWindow is
    /// already null by the time it gets there).
    /// </summary>
    public async Task HandleStopAsync(string requestId)
    {
        Logger.Log($"VisualModeManager: HandleStopAsync requestId={requestId} matchesActive={_activeRequestId == requestId}");
        if (_activeRequestId != requestId) return;
        _activeWindow?.TriggerStop();
        await CloseActiveWindowAsync();
    }

    private Task CloseActiveWindowAsync()
    {
        var window = _activeWindow;
        _activeWindow = null;
        _activeRequestId = null;
        if (window == null)
        {
            Logger.Log("VisualModeManager: CloseActiveWindowAsync -- no active window, nothing to do.");
            return Task.CompletedTask;
        }
        Logger.Log("VisualModeManager: CloseActiveWindowAsync -- closing active window.");
        try { window.Close(); } catch (Exception ex) { Logger.Log($"VisualModeManager: window.Close() threw: {ex}"); }
        return Task.CompletedTask;
    }
}

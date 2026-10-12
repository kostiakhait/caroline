using System;
using System.Collections.Generic;
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
///
/// Two things do look past the clip playing now (2026-10-11), because chat.js
/// reads a long reply in pieces and each piece is a request of its own. Measured
/// live, every boundary between two pieces was 4.0s of nothing: the 1s post-roll,
/// 2.8s to render the next piece, a new window. So the next piece can be rendered
/// while the current one is still playing (HandlePrepareAsync), and a clip that is
/// ready within the previous clip's post-roll takes that clip's window over
/// (StartPostRoll) instead of one window vanishing and another appearing.
/// </summary>
public sealed class VisualModeManager
{
    /// <summary>"через 1 секунду после окончания анимации исчезать" -- how long the window
    /// outlives its clip, and so also how long the next clip has to take it over. This is
    /// for a clip played by a click on a message's speaker button, and for a window the
    /// user has just pressed Stop on. A dialogue's window is on no timer at all -- see
    /// StayForNextClip.</summary>
    private static readonly TimeSpan PostRoll = TimeSpan.FromSeconds(1);

    /// <summary>chat.js prepares one item ahead; the second slot is slack for a prepare that
    /// crosses a cancel on the wire. Anything beyond that is a request nobody will play.</summary>
    private const int MaxRendersAhead = 2;

    private readonly string _tempDir;
    private XcfaRenderer.Renderer.PreparedModel? _prepared;
    private byte[]? _staticPng;
    private readonly object _prepGate = new();

    private VisualModeWindow? _activeWindow;
    private string? _activeRequestId;

    /// <summary>One clip's render. Done is false when it failed (already logged); it never faults.</summary>
    private sealed record RenderJob(string RequestId, byte[] Audio, string AudioPath, string OutputPath, Task<bool> Done);

    private readonly List<RenderJob> _rendersAhead = new();
    private Task _lastRender = Task.CompletedTask;

    /// <summary>The window of a clip that has ended: in its post-roll (StartPostRoll), or
    /// standing between two remarks of a dialogue (StayForNextClip).</summary>
    private VisualModeWindow? _lingeringWindow;
    private int _postRollGeneration;

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
    /// Starts rendering one clip in the background. Renders run one at a time and in the
    /// order they were asked for, so the clip about to play never waits behind the one
    /// being prepared for after it.
    /// </summary>
    private RenderJob StartRender(string requestId, byte[] audioBytes, XcfaRenderer.Renderer.PreparedModel prepared, string caller)
    {
        var audioPath = Path.Combine(_tempDir, $"{requestId}.mp3");
        var outputPath = Path.Combine(_tempDir, $"{requestId}.webm");
        var previous = _lastRender;
        var done = Task.Run(async () =>
        {
            await previous;
            try
            {
                await File.WriteAllBytesAsync(audioPath, audioBytes);
                Logger.Log($"VisualModeManager: {caller} requestId={requestId} render starting");
                var renderSw = Stopwatch.StartNew();
                prepared.Render(audioPath, outputPath,
                    new XcfaRenderer.RenderOptions { RemoveBg = true, Height = 480, Ffmpeg = FfmpegPath });
                Logger.Log($"VisualModeManager: {caller} requestId={requestId} render finished in {renderSw.Elapsed.TotalSeconds:F1}s");
                return true;
            }
            catch (Exception ex)
            {
                Logger.Log($"VisualModeManager: {caller} requestId={requestId} render failed: {ex}");
                return false;
            }
        });
        _lastRender = done;
        return new RenderJob(requestId, audioBytes, audioPath, outputPath, done);
    }

    private RenderJob? TakeRenderAhead(string requestId)
    {
        var index = _rendersAhead.FindIndex(job => job.RequestId == requestId);
        if (index < 0) return null;
        var job = _rendersAhead[index];
        _rendersAhead.RemoveAt(index);
        return job;
    }

    /// <summary>Deletes the files of a render nobody will play, once it is done writing them.</summary>
    private static async Task DiscardRenderAsync(RenderJob job)
    {
        await job.Done;
        SafeDeleteTemp(job.AudioPath);
        SafeDeleteTemp(job.OutputPath);
    }

    /// <summary>
    /// The audio of the item chat.js will play NEXT, sent while the current one is still
    /// playing (2026-10-11): rendering it now, not when its turn comes, takes the render
    /// out of the pause between two clips. This only renders -- no window, and nothing
    /// becomes "current"; HandleAudioAsync for the same requestId picks the result up.
    /// </summary>
    public Task HandlePrepareAsync(string requestId, byte[] audioBytes)
    {
        Logger.Log($"VisualModeManager: HandlePrepareAsync requestId={requestId} bytes={audioBytes.Length}");
        XcfaRenderer.Renderer.PreparedModel? prepared;
        lock (_prepGate) prepared = _prepared;
        if (prepared == null)
        {
            Logger.Log("VisualModeManager: HandlePrepareAsync -- model not warmed (yet), nothing prepared.");
            return Task.CompletedTask;
        }
        if (_rendersAhead.Exists(job => job.RequestId == requestId)) return Task.CompletedTask;
        while (_rendersAhead.Count >= MaxRendersAhead)
        {
            var stale = _rendersAhead[0];
            _rendersAhead.RemoveAt(0);
            Logger.Log($"VisualModeManager: HandlePrepareAsync -- dropping the render prepared for {stale.RequestId}, it was never played");
            _ = DiscardRenderAsync(stale);
        }
        _rendersAhead.Add(StartRender(requestId, audioBytes, prepared, "HandlePrepareAsync"));
        return Task.CompletedTask;
    }

    /// <summary>
    /// TTS audio is ready -- renders and plays it. Resolves once playback is done (the 1s
    /// post-roll no longer holds it up -- see StartPostRoll). Played=false means the
    /// caller (MainWindow) should tell the client to fall back to plain
    /// audio instead, so a not-yet-warmed model or a render failure degrades to normal
    /// TTS playback instead of silence. Confirmed live (2026-09-03) as a real gap: right
    /// after a fresh restart, the model can still be mid-warm-up when the first voice
    /// reply comes in, and this used to just close the window with nothing audible at
    /// all -- worse than not having Visual Mode.
    /// </summary>
    public async Task<VisualPlaybackResult> HandleAudioAsync(string requestId, byte[] audioBytes, bool moreToCome = false, bool dialogue = false)
    {
        Logger.Log($"VisualModeManager: HandleAudioAsync requestId={requestId} bytes={audioBytes.Length}");
        var ahead = TakeRenderAhead(requestId);
        // Request ids are per chat tab ("tts-N"), and tabs share this one manager: a render
        // prepared for another tab's request of the same name must not be played as this one.
        if (ahead != null && !audioBytes.AsSpan().SequenceEqual(ahead.Audio))
        {
            Logger.Log($"VisualModeManager: HandleAudioAsync requestId={requestId} -- the render prepared under this id is for other audio, rendering anew");
            _ = DiscardRenderAsync(ahead);
            ahead = null;
        }
        if (_activeRequestId != requestId)
        {
            // Already stopped/cancelled for this request -- not a failure, and definitely
            // not something a plain-audio fallback should un-silence after the fact.
            Logger.Log("VisualModeManager: HandleAudioAsync -- requestId no longer current, ignoring (no fallback).");
            if (ahead != null) _ = DiscardRenderAsync(ahead);
            return new VisualPlaybackResult(Played: true, StoppedByUser: false);
        }

        XcfaRenderer.Renderer.PreparedModel? prepared;
        lock (_prepGate) prepared = _prepared;
        if (prepared == null)
        {
            Logger.Log("VisualModeManager: HandleAudioAsync -- model not warmed (yet), signaling fallback.");
            if (ahead != null) _ = DiscardRenderAsync(ahead);
            return new VisualPlaybackResult(Played: false, StoppedByUser: false);
        }

        if (ahead != null) Logger.Log($"VisualModeManager: HandleAudioAsync requestId={requestId} using the render prepared ahead ({(ahead.Done.IsCompleted ? "ready" : "still rendering")})");
        var job = ahead ?? StartRender(requestId, audioBytes, prepared, "HandleAudioAsync");
        var audioPath = job.AudioPath;
        var outputPath = job.OutputPath;
        if (!await job.Done)
        {
            Logger.Log($"VisualModeManager: HandleAudioAsync requestId={requestId} has no render -- signaling fallback.");
            SafeDeleteTemp(audioPath);
            SafeDeleteTemp(outputPath);
            return new VisualPlaybackResult(Played: false, StoppedByUser: false);
        }

        if (_activeRequestId != requestId)
        {
            // Stopped/cancelled while rendering -- an intentional interruption, not a
            // failure, so no fallback either (nothing left to play it into).
            Logger.Log($"VisualModeManager: HandleAudioAsync requestId={requestId} no longer current after render, skipping playback (no fallback)");
            SafeDeleteTemp(audioPath);
            SafeDeleteTemp(outputPath);
            return new VisualPlaybackResult(Played: true, StoppedByUser: false);
        }

        var window = _lingeringWindow;
        if (window != null)
        {
            // The previous clip's window is still up (in its post-roll, or standing in a
            // dialogue) and already initialized, so this clip simply continues in it.
            _lingeringWindow = null;
            _activeWindow = window;
            Logger.Log($"VisualModeManager: HandleAudioAsync requestId={requestId} taking over the previous clip's window");
        }
        else
        {
            // The window is created HERE, right before actual playback -- per explicit
            // instruction (2026-09-03, the SECOND correction on this point): even an
            // invisible, pre-warmed window sitting around since TTS-generation-start was
            // "too early." Show() still has to happen before InitializeAsync (see that
            // method's own doc comment for why -- WebView2 needs a real HWND to attach
            // to), so there's an unavoidable ~0.3-0.5s pause here while the window comes
            // up, but that's the earliest point that's actually correct to show it at.
            window = new VisualModeWindow();
            // Stop pressed while nothing is playing: the model standing between two
            // remarks is sent away.
            var created = window;
            created.DismissRequested += () =>
            {
                if (_lingeringWindow != created) return;
                Logger.Log("VisualModeManager: the idle window was dismissed with its Stop button.");
                CloseWindow(created);
            };
            _activeWindow = window;
            window.Show();
            window.SetVisuallyHidden(true); // hidden for the brief Init/navigate window, revealed just below once actually playing
            Logger.Log($"VisualModeManager: HandleAudioAsync requestId={requestId} window created and shown, calling InitializeAsync");
            await window.InitializeAsync();

            if (_activeRequestId != requestId)
            {
                Logger.Log($"VisualModeManager: HandleAudioAsync requestId={requestId} no longer current after InitializeAsync, closing without playing (no fallback)");
                CloseWindow(window);
                SafeDeleteTemp(audioPath);
                SafeDeleteTemp(outputPath);
                return new VisualPlaybackResult(Played: true, StoppedByUser: false);
            }
            if (!window.IsReady)
            {
                Logger.Log("VisualModeManager: HandleAudioAsync -- window failed to become ready (InitializeAsync timed out), closing, signaling fallback.");
                CloseWindow(window);
                SafeDeleteTemp(audioPath);
                SafeDeleteTemp(outputPath);
                return new VisualPlaybackResult(Played: false, StoppedByUser: false);
            }
        }

        var played = false;
        var playbackEnded = new TaskCompletionSource();
        void OnEnded() => playbackEnded.TrySetResult();
        window.PlaybackEnded += OnEnded;

        try
        {
            Logger.Log($"VisualModeManager: HandleAudioAsync requestId={requestId} playback starting");
            window.SetVisuallyHidden(false);
            // The controls bar from the first piece on (see VisualModeWindow.MoreToCome) --
            // and for every clip of a dialogue: a window that stays needs its Stop.
            window.MoreToCome = moreToCome || dialogue;
            // Once a dialogue's window, it stays one: a speaker button clicked in between
            // must not put it back on the one-second timer.
            if (dialogue) window.StaysUp = true;
            await window.PlayVideoAsync(outputPath);
            await playbackEnded.Task;
            played = true;
            var how = _activeRequestId != requestId ? "via stop" : window.StoppedByUser ? "via the window's own stop button" : "naturally";
            Logger.Log($"VisualModeManager: HandleAudioAsync requestId={requestId} playback ended ({how})");
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

        // Not the active window any more: HandleStopAsync closed it, nothing left to do here.
        if (_activeWindow == window)
        {
            // A window that failed to play is not handed on to the next clip.
            if (!played) CloseWindow(window);
            // Stopped by its own button, the window goes after the usual second even in a
            // dialogue: that is the user closing it.
            else if (window.StaysUp && !window.StoppedByUser) StayForNextClip(window);
            else StartPostRoll(window);
        }
        return new VisualPlaybackResult(played, played && window.StoppedByUser);
    }

    /// <summary>
    /// "через 1 секунду после окончания анимации исчезать" -- the window closes PostRoll
    /// after its clip ended, whether that was naturally or via the window's own Stop
    /// button. HandleAudioAsync no longer waits that second out (2026-10-11): chat.js
    /// learns right away that the clip is over, and if its next item is ready before the
    /// post-roll runs out (it is when it was rendered ahead -- see HandlePrepareAsync),
    /// that clip takes this window over. Live before this, the pieces of one reply each
    /// closed the window and opened a new one, 4.0s apart.
    /// </summary>
    private void StartPostRoll(VisualModeWindow window)
    {
        _activeWindow = null;
        _lingeringWindow = window;
        _ = CloseAfterPostRollAsync(window, ++_postRollGeneration);
    }

    /// <summary>
    /// Per explicit instruction (2026-10-11, and again the same day when a 180s silence
    /// limit was tried here): in a continuous spoken dialogue the animation window is
    /// closed by the user and by nothing else -- "закрывается ТОЛЬКО принудительно
    /// пользователем (Стоп на окне или в чате) и никак иначе ... автозакрытия по таймеру
    /// быть не должно". A clip that is part of the dialogue (chat.js says so: a reply or
    /// a remark spoken on its own, not a click on a message's speaker button) leaves the
    /// window standing for the next thing said, however long that takes. It goes by its
    /// own Stop (VisualModeWindow.DismissRequested), by the chat's Stop
    /// (HandleDismissAsync), or with the app.
    /// </summary>
    private void StayForNextClip(VisualModeWindow window)
    {
        _activeWindow = null;
        _lingeringWindow = window;
        var generation = ++_postRollGeneration;
        Logger.Log("VisualModeManager: dialogue -- the window stays for the next clip, until the user closes it.");
        window.Idle = true;
        // Between two remarks the model stands in its rest pose, not frozen on the
        // last frame of the last word (seen live: mouth open, for as long as the pause).
        byte[]? rest;
        lock (_prepGate) rest = _staticPng;
        if (rest != null) _ = ShowRestPoseAsync(window, rest, generation);
    }

    private async Task ShowRestPoseAsync(VisualModeWindow window, byte[] png, int generation)
    {
        try
        {
            // Not if the next clip has taken the window over by now.
            if (_lingeringWindow != window || _postRollGeneration != generation) return;
            await window.ShowStaticAsync("data:image/png;base64," + Convert.ToBase64String(png));
        }
        catch (Exception ex)
        {
            Logger.Log($"VisualModeManager: showing the rest pose failed: {ex.Message}");
        }
    }

    private async Task CloseAfterPostRollAsync(VisualModeWindow window, int generation)
    {
        await Task.Delay(PostRoll);
        // Taken over by the next clip (and possibly lingering again, for THAT clip's
        // post-roll -- hence the generation), or closed by a stop in the meantime.
        if (_lingeringWindow != window || _postRollGeneration != generation) return;
        Logger.Log("VisualModeManager: post-roll over, no clip took the window over.");
        CloseWindow(window);
    }

    /// <summary>The request is not going to be played: TTS synthesis itself failed (no window
    /// exists yet at this point -- see HandleStartAsync), or chat.js dropped a queued item
    /// whose render was already prepared ahead.</summary>
    public Task HandleCancelAsync(string requestId)
    {
        Logger.Log($"VisualModeManager: HandleCancelAsync requestId={requestId} matchesActive={_activeRequestId == requestId}");
        var ahead = TakeRenderAhead(requestId);
        if (ahead != null) _ = DiscardRenderAsync(ahead);
        if (_activeRequestId != requestId) return Task.CompletedTask;
        _activeRequestId = null;
        CloseWindow(_activeWindow);
        return Task.CompletedTask;
    }

    /// <summary>
    /// User-requested stop (the main chat window's Stop / queue-clear) -- closes
    /// immediately, unlike the natural end-of-playback path's 1s post-roll.
    /// TriggerStop() first so a concurrently in-flight HandleAudioAsync (awaiting
    /// playbackEnded) unblocks and runs its own cleanup instead of hanging forever.
    /// That continuation can run right inside TriggerStop() and hand the window to
    /// the post-roll, so the window is closed by reference, wherever it is by then --
    /// which is what actually makes it disappear right away. A request stopped before
    /// its own playback began has no window yet; the previous clip's window, if it is
    /// still in its post-roll, goes instead.
    /// </summary>
    public Task HandleStopAsync(string requestId)
    {
        Logger.Log($"VisualModeManager: HandleStopAsync requestId={requestId} matchesActive={_activeRequestId == requestId}");
        if (_activeRequestId != requestId) return Task.CompletedTask;
        _activeRequestId = null;
        var window = _activeWindow;
        window?.TriggerStop();
        CloseWindow(window ?? _lingeringWindow);
        return Task.CompletedTask;
    }

    /// <summary>
    /// The chat's own Stop, or Visual Mode switched off: whatever window is up goes, a
    /// clip playing in it or not. chat.js stops the clip it is playing by its request id
    /// (HandleStopAsync); this is for the window no request owns any more -- the one
    /// standing between two remarks of a dialogue, which nothing else would close.
    /// </summary>
    public Task HandleDismissAsync()
    {
        var window = _activeWindow;
        Logger.Log($"VisualModeManager: HandleDismissAsync -- {(window != null ? "a clip is playing" : _lingeringWindow != null ? "the window is standing idle" : "no window")}");
        _activeRequestId = null;
        window?.TriggerStop();
        CloseWindow(window ?? _lingeringWindow);
        return Task.CompletedTask;
    }

    private void CloseWindow(VisualModeWindow? window)
    {
        if (window == null)
        {
            Logger.Log("VisualModeManager: CloseWindow -- no window, nothing to do.");
            return;
        }
        if (_activeWindow == window) _activeWindow = null;
        if (_lingeringWindow == window) _lingeringWindow = null;
        Logger.Log("VisualModeManager: CloseWindow -- closing the window.");
        try { window.Close(); } catch (Exception ex) { Logger.Log($"VisualModeManager: window.Close() threw: {ex}"); }
    }
}

/// <summary>How VisualModeManager.HandleAudioAsync ended. Played=false: nothing was shown
/// or heard, the client should fall back to plain audio. StoppedByUser: cut short by the
/// talking-head window's own Stop button, which the chat side cannot see otherwise.</summary>
public readonly record struct VisualPlaybackResult(bool Played, bool StoppedByUser);

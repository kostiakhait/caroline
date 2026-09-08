namespace XcfaRenderer;

/// <summary>Matches _render_worker.py's render_mode arg. Frame is the production default (see Renderer's class doc).</summary>
public enum RenderMode { Frame, Segment }

/// <summary>Named resolution presets, matching main()'s _PRESETS dict -- resolves to a target height (width follows from aspect ratio).</summary>
public static class ResolutionPresets
{
    public static readonly IReadOnlyDictionary<string, int> Values = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase)
    {
        ["240p"] = 240, ["360p"] = 360, ["480p"] = 480, ["540p"] = 540,
        ["720p"] = 720, ["1080p"] = 1080, ["1440p"] = 1440, ["2160p"] = 2160, ["4k"] = 2160,
    };
}

/// <summary>Rendering knobs, mirroring _render_worker.py's main() args (subset relevant to a library caller).</summary>
public sealed class RenderOptions
{
    public int Fps { get; init; } = 25;
    public int? Width { get; init; }
    public int? Height { get; init; }
    /// <summary>Named preset ("720p", "1080p", ...) -- only applied when neither Width nor Height is set, matching main()'s priority order.</summary>
    public string? Resolution { get; init; }
    public double Scale { get; init; } = 1.0;
    public string? VideoBitrate { get; init; }
    /// <summary>Requires the .xcfa to have baked alpha masks (Catalog.HasBg) -- throws otherwise.</summary>
    public bool RemoveBg { get; init; }
    /// <summary>CSS-style color name or #RGB/#RRGGBB hex. Requires baked alpha masks. Takes priority over RemoveBg if both are set.</summary>
    public string? BgColor { get; init; }
    public bool MicroMovement { get; init; } = true;
    public double StyleIntensity { get; init; } = 1.0;
    public string Ffmpeg { get; init; } = "ffmpeg";
    public RenderMode Mode { get; init; } = RenderMode.Frame;
    /// <summary>Feature-lead time in ms, overriding the 250ms default (_FEATURE_LEAD_MS). Direct port of main()'s lead_ms arg.</summary>
    public int LeadMs { get; init; } = 250;
    /// <summary>On-disk audio-feature cache directory (see FeatureCache.cs) -- set to null to disable caching. Direct port of _feat_cache_dir's caching behavior (Config.STORAGE-backed there; OS temp dir here, see AudioFeatures.DefaultFeatureCacheDir).</summary>
    public string? FeatureCacheDir { get; init; } = AudioFeatures.DefaultFeatureCacheDir;
    /// <summary>
    /// Minimum silence-run length (seconds) needed to activate the living-pause
    /// ENTRY/SUSTAIN/EXIT state machine; shorter pauses render via the normal
    /// per-frame cosine path instead. Python hardcodes this to fps*2 (2.0s,
    /// `min_sil_frames = fps * 2`) -- exposed as a tunable here since 2s can be
    /// too slow to react for TTS-paced speech with short inter-sentence gaps.
    /// </summary>
    public double MinSilenceSeconds { get; init; } = 2.0;
    /// <summary>
    /// Duration (seconds) of the EXIT ramp (mouth reopening back to normal
    /// speech) at the end of a detected silence run. Python hardcodes this
    /// to ~1 second (SilExitMuls' 6 steps at fps/6 frames each, and
    /// sustain_run_end reserves exactly fps frames for it). If a detected
    /// pause is short (close to MinSilenceSeconds), a 1-second EXIT ramp can
    /// eat most or all of the pause, leaving the mouth still mid-reopening
    /// when speech actually resumes -- shorten this to make EXIT finish
    /// well before the pause ends.
    /// </summary>
    public double SilenceExitSeconds { get; init; } = 1.0;
}

/// <summary>
/// Top-level render orchestrator: given a .xcfa model and a speech audio
/// clip, produces a talking-head video. Ports BOTH of _render_worker.py's
/// render modes (RenderOptions.Mode): the production default "frame" mode
/// (RenderFrameLevel, direct port of _render_frame_level_mode -- per-frame
/// cosine phoneme matching with hysteresis + a jump cooldown, plus its
/// 3-phase living-pause silence state machine) and "segment" mode
/// (RenderSegmentMode, direct port of _render_segment_mode -- whole-segment
/// selection via lookahead + stitch quality, intra-segment optical-flow
/// interpolation from stored flow blobs, live Farneback morph between
/// segments). Frame mode is what production actually runs (neither
/// vgen.html nor Api2FaceModelCommands.cmdV2RenderFaceModel ever pass
/// render_mode="segment"), so it's this library's default too.
///
/// Outside of silence runs, frame mode never runs live optical flow at all:
/// it plays back stored JPEG frames directly, occasionally jumping to a
/// different stored frame (no interpolation) when find_best_frame's
/// hysteresis decides a phonemically closer match is worth the cut. That --
/// not a hidden live-CV optimization -- is most of why production renders in
/// seconds on a weak, GPU-less VM: most frames are a JPEG decode plus a
/// cosine-similarity lookup, nothing more; live Farneback only runs during
/// silence transitions, on short single-segment morphs.
/// </summary>
public static class Renderer
{
    private const float SwitchThreshold = 0.12f;

    public static void Render(string xcfaPath, string audioPath, string outputPath, RenderOptions? options = null)
    {
        options ??= new RenderOptions();
        using var model = Prepare(xcfaPath, outputPath, options);
        model.Render(audioPath, outputPath, options);
    }

    /// <summary>
    /// Renders multiple audio clips against the same already-open model in
    /// one call, sharing the model, segment/silence libraries, and decoded-
    /// frame cache across jobs -- direct port of _render_worker.py's batch
    /// path (main()'s `jobs_list` branch + _render_audio_one). Matches one
    /// real quirk of the Python original faithfully rather than "fixing"
    /// it: background handling (color-fill / blacken / webm-alpha) is
    /// decided ONCE, from the FIRST job's output path, and reused for every
    /// job in the batch regardless of that job's own extension -- exactly
    /// like main() building `_bg_masks`/`base_img` once before the batch
    /// dispatch loop, using the top-level `output_path` (which for
    /// `jobs_list` resolves to `jobs_list[0]["output_path"]`, see main():152).
    /// </summary>
    public static void RenderBatch(string xcfaPath, IReadOnlyList<(string AudioPath, string OutputPath)> jobs, RenderOptions? options = null)
    {
        if (jobs.Count == 0) return;
        options ??= new RenderOptions();
        using var model = Prepare(xcfaPath, jobs[0].OutputPath, options);
        foreach (var (audioPath, outputPath) in jobs)
            model.Render(audioPath, outputPath, options);
    }

    /// <summary>
    /// Everything about a .xcfa that's independent of which audio clip is
    /// being rendered: file parsing, base-image decode/resize, bg-mask
    /// preparation, segment/silence libraries, decoded-frame cache. This is
    /// the "one-time preparation" step -- expensive relative to a single
    /// render on a large model (decodes every silence-tagged segment's
    /// frames up front, for instance) but reusable across any number of
    /// PreparedModel.Render calls, matching main()'s ctx dict / how
    /// RenderBatch already shared it across jobs before this was made
    /// public. Call Prepare() once, then Render() as many times as needed,
    /// then Dispose() (or `using`) when done.
    /// </summary>
    public sealed class PreparedModel : IDisposable
    {
        // No `required` here: a required init-only property on a public type cannot have an
        // internal setter (CS9032). Visibility is instead enforced by the private constructor
        // below -- only Prepare() (same class, so it can see internal/private members) can ever
        // build one of these; external callers only ever get a PreparedModel back from Prepare().
        internal PreparedModel() { }

        internal XcfaFile Xcfa { get; init; } = null!;
        internal XcfaCatalog Catalog { get; init; } = null!;
        internal Frame BaseFrame { get; init; } = null!;
        internal int OutW { get; init; }
        internal int OutH { get; init; }
        internal bool WantsBg { get; init; }
        internal bool UseAlphaChannel { get; init; }
        internal byte[]? BaseMask { get; init; }
        internal (int X, int Y, int W, int H)? FaceRegion { get; init; }
        internal SegmentLibrary Library { get; init; } = null!;
        internal SilenceLibrary SilenceLibrary { get; init; } = null!;
        internal Func<int, Frame[]> GetDecodedSegment { get; init; } = null!;

        /// <summary>Renders one audio clip against this already-prepared model.</summary>
        public void Render(string audioPath, string outputPath, RenderOptions? options = null) =>
            RenderJob(this, audioPath, outputPath, options ?? new RenderOptions());

        /// <summary>
        /// PNG-encoded bytes of the model's resting/"silence" frame -- BaseFrame with
        /// BaseMask applied as a real alpha channel (not baked into RGB), for a caller
        /// that wants to show a static image before the first Render() call completes.
        /// Only meaningful when this model has baked alpha masks (WantsBg/BaseMask set,
        /// i.e. RenderOptions.RemoveBg was true in Prepare()) -- otherwise the frame is
        /// opaque, which is still returned (just without transparency).
        /// </summary>
        public byte[] GetStaticFramePng()
        {
            var w = BaseFrame.Width;
            var h = BaseFrame.Height;
            using var bitmap = new SkiaSharp.SKBitmap(new SkiaSharp.SKImageInfo(w, h, SkiaSharp.SKColorType.Rgba8888, SkiaSharp.SKAlphaType.Unpremul));
            var span = bitmap.GetPixelSpan();
            for (var i = 0; i < w * h; i++)
            {
                var o = i * 3;
                var dst = i * 4;
                span[dst] = BaseFrame.Bgr[o + 2];     // R
                span[dst + 1] = BaseFrame.Bgr[o + 1]; // G
                span[dst + 2] = BaseFrame.Bgr[o];     // B
                span[dst + 3] = BaseMask?[i] ?? 255;  // A
            }
            using var image = SkiaSharp.SKImage.FromBitmap(bitmap);
            using var data = image.Encode(SkiaSharp.SKEncodedImageFormat.Png, 100);
            return data.ToArray();
        }

        public void Dispose() => Xcfa.Dispose();
    }

    /// <summary>
    /// Runs the one-time model-preparation step (see PreparedModel's doc)
    /// without rendering anything yet. representativeOutputPath only
    /// matters for its file extension (webm vs not), which decides
    /// alpha-channel vs color-fill/blacken bg handling for every
    /// subsequent Render() call on the result -- pass the extension you
    /// actually intend to render with.
    /// </summary>
    public static PreparedModel Prepare(string xcfaPath, string representativeOutputPath, RenderOptions? options = null)
    {
        options ??= new RenderOptions();
        var xcfa = XcfaFile.Open(xcfaPath);
        var catalog = xcfa.Catalog;
        if (catalog.Segments.Length == 0)
        {
            xcfa.Dispose();
            throw new InvalidOperationException("Model has no segments.");
        }

        var baseFrame = Frame.DecodeJpegNative(xcfa.ReadBaseImage());
        var (outW, outH) = ResolveOutputSize(baseFrame.Width, baseFrame.Height, options);
        if (outW != baseFrame.Width || outH != baseFrame.Height)
            baseFrame = ResizeFrame(baseFrame, outW, outH);

        var wantsBg = options.RemoveBg || options.BgColor is not null;
        if (wantsBg && !catalog.HasBg)
        {
            xcfa.Dispose();
            throw new InvalidOperationException(
                "RemoveBg/BgColor requested but this .xcfa has no baked alpha masks (Catalog.HasBg is false). " +
                "Bake masks offline first (XcfaBgUpgrader.py's upgrade workflow).");
        }

        var useAlphaChannel = wantsBg && options.BgColor is null
            && representativeOutputPath.EndsWith(".webm", StringComparison.OrdinalIgnoreCase);
        byte[]? baseMask = wantsBg ? AlphaCompositor.LoadBaseMask(xcfa, outW, outH) : null;
        var fillColor = options.BgColor is not null ? ParseBgColorBgr(options.BgColor) : ((byte, byte, byte)?)null;

        if (baseMask is not null)
        {
            if (fillColor is { } fc) AlphaCompositor.ApplyColorFill(baseFrame, baseMask, fc);
            else if (!useAlphaChannel) AlphaCompositor.ApplyBlacken(baseFrame, baseMask);
            // else: webm+alpha output keeps base RGB untouched; transparency comes from the alpha channel
            // (see Encoder -- matches _FfmpegAlphaPipeWriter's own static-base-mask behavior exactly).
        }

        (int X, int Y, int W, int H)? faceRegion = null;
        if (catalog.FaceRegion is { } fr)
            faceRegion = ((int)(fr.XFrac * outW), (int)(fr.YFrac * outH), (int)(fr.WFrac * outW), (int)(fr.HFrac * outH));

        var library = new SegmentLibrary(catalog);
        var decodedSegCache = new Dictionary<int, Frame[]>();
        var maskCache = wantsBg ? new Dictionary<int, byte[]?[]>() : null;

        Frame ApplyBgIfNeeded(int segId, int frameIdx, Frame raw)
        {
            if (!wantsBg || useAlphaChannel) return raw; // alpha-channel output keeps RGB untouched everywhere
            var seg = catalog.Segments.First(s => s.Id == segId);
            var mask = LoadSegmentMask(xcfa, seg, frameIdx, outW, outH, maskCache!);
            if (mask is null) return raw;
            var result = raw.Clone();
            if (fillColor is { } fc) AlphaCompositor.ApplyColorFill(result, mask, fc);
            else AlphaCompositor.ApplyBlacken(result, mask);
            return result;
        }

        Frame[] GetDecodedSegment(int segId)
        {
            if (decodedSegCache.TryGetValue(segId, out var cached)) return cached;
            var seg = catalog.Segments.First(s => s.Id == segId);
            var frames = new Frame[seg.Frames.Length];
            for (var i = 0; i < seg.Frames.Length; i++)
                frames[i] = ApplyBgIfNeeded(segId, i, Frame.DecodeJpeg(xcfa.ReadFrame(seg, i), outW, outH));
            decodedSegCache[segId] = frames;
            return frames;
        }

        // Silence-tagged segments, pre-decoded and bg-processed the same way as GetDecodedSegment,
        // grouped by mul -- feeds the living-pause state machine below. Direct port of
        // precompute_silence_flows's new-model (tagged-segment) branch; see SilenceLibrary.cs.
        var silenceLibrary = SilenceLibrary.Build(xcfa, outW, outH, ApplyBgIfNeeded);

        return new PreparedModel
        {
            Xcfa = xcfa, Catalog = catalog, BaseFrame = baseFrame, OutW = outW, OutH = outH,
            WantsBg = wantsBg, UseAlphaChannel = useAlphaChannel, BaseMask = baseMask, FaceRegion = faceRegion,
            Library = library, SilenceLibrary = silenceLibrary, GetDecodedSegment = GetDecodedSegment,
        };
    }

    private static void RenderJob(PreparedModel model, string audioPath, string outputPath, RenderOptions options)
    {
        var audioRaw = AudioFeatures.LoadPcm(audioPath, options.Ffmpeg);
        var totalSec = audioRaw.Length / (double)AudioFeatures.SampleRate;
        var nFrames = Math.Max(1, (int)(totalSec * options.Fps));
        var windowSamples = Math.Max(256, AudioFeatures.SampleRate / Math.Max(1, (int)model.Catalog.Fps));
        var (feats, energies) = AudioFeatures.ComputeStreaming(audioRaw, nFrames, options.Fps, windowSamples, options.LeadMs,
            sampleFps: model.Catalog.Fps, cacheDir: options.FeatureCacheDir);

        using var encoder = Encoder.Open(model.OutW, model.OutH, options.Fps, audioPath, outputPath,
            model.UseAlphaChannel, options.Ffmpeg, options.VideoBitrate);

        if (options.Mode == RenderMode.Frame)
        {
            RenderFrameLevel(
                writer: encoder, nFrames: nFrames, fps: options.Fps, sampleFps: model.Catalog.Fps,
                library: model.Library, silenceLibrary: model.SilenceLibrary, feats: feats, energies: energies,
                baseFrame: model.BaseFrame, faceRegion: model.FaceRegion, getDecodedSegment: model.GetDecodedSegment,
                useAlphaChannel: model.UseAlphaChannel, baseMask: model.BaseMask,
                micro: options.MicroMovement, styleIntensity: options.StyleIntensity,
                minSilenceSeconds: options.MinSilenceSeconds, exitSeconds: options.SilenceExitSeconds);
        }
        else
        {
            var interpPerPair = Math.Max(1, (int)Math.Round((double)options.Fps / model.Catalog.Fps));
            var minSegFrames = Math.Max(interpPerPair * 9, (int)(options.Fps * 0.8));
            var lookaheadFrames = Math.Max(1, options.Fps);
            const int silenceOnset = 3;

            RenderSegmentMode(
                writer: encoder, nFrames: nFrames, fps: options.Fps,
                library: model.Library, silenceLibrary: model.SilenceLibrary, feats: feats, energies: energies,
                baseFrame: model.BaseFrame, faceRegion: model.FaceRegion, getDecodedSegment: model.GetDecodedSegment,
                xcfa: model.Xcfa, catalog: model.Catalog, outW: model.OutW, outH: model.OutH,
                useAlphaChannel: model.UseAlphaChannel, baseMask: model.BaseMask,
                micro: options.MicroMovement, styleIntensity: options.StyleIntensity,
                silenceThresh: 0.005f, interpPerPair: interpPerPair, minSegFrames: minSegFrames,
                silenceOnset: silenceOnset, lookaheadFrames: lookaheadFrames);
        }

        encoder.Close();
    }

    // _SIL_ENTRY_MULS / _SIL_SUSTAIN_MULS / _SIL_EXIT_MULS, verbatim from _render_worker.py:1845-1849.
    // Entry:   1 second, step down from ~0.5 to 0.0 (6 equal steps)
    // Sustain: triangle 0.0->0.1->0.2->0.3->0.2->0.1 (repeating cycle)
    // Exit:    1 second, step up from 0.0 to ~0.5 (6 equal steps)
    private static readonly float[] SilEntryMuls = { 0.5f, 0.4f, 0.3f, 0.2f, 0.1f, 0.0f };
    private static readonly float[] SilSustainMuls = { 0.0f, 0.1f, 0.2f, 0.3f, 0.2f, 0.1f };
    private static readonly float[] SilExitMuls = { 0.0f, 0.1f, 0.2f, 0.3f, 0.4f, 0.5f };

    /// <summary>
    /// Per-frame phoneme matching with hysteresis + jump cooldown, plus the
    /// 3-phase living-pause silence state machine (ENTRY/SUSTAIN/EXIT).
    /// Direct, line-for-line port of _render_worker.py's
    /// _render_frame_level_mode (the production default render_mode="frame").
    /// </summary>
    private static void RenderFrameLevel(
        Encoder writer, int nFrames, int fps, float sampleFps,
        SegmentLibrary library, SilenceLibrary silenceLibrary, AudioFeatures.FeatureStream feats, float[] energies,
        Frame baseFrame, (int X, int Y, int W, int H)? faceRegion, Func<int, Frame[]> getDecodedSegment,
        bool useAlphaChannel, byte[]? baseMask, bool micro, double styleIntensity, double minSilenceSeconds = 2.0, double exitSeconds = 1.0)
    {
        const float silenceThresh = 0.005f;
        // Direct port of _render_frame_level_mode's literal "JUMP_COOLDOWN = max(1, round(fps / 5))" --
        // that 5 is a hardcoded constant, NOT sample_fps (an earlier version of this port used sampleFps
        // here by mistake, which for a 25fps-sampled model like CarolineB gave jumpCooldown=1 instead of
        // 5 -- re-evaluating find_best_frame on nearly every single frame instead of every 5th, starving
        // the switch_threshold hysteresis of any chance to keep playback on one segment and causing
        // constant segment-to-segment jumping, i.e. exactly the "monstrously fast/chaotic lip movement"
        // reported after the first port).
        var jumpCooldown = Math.Max(1, (int)Math.Round(fps / 5.0));
        const float switchThreshold = SwitchThreshold;

        var hasSilenceSegs = silenceLibrary.HasData;
        var minSilFrames = Math.Max(1, (int)Math.Round(fps * minSilenceSeconds)); // RenderOptions.MinSilenceSeconds (Python hardcodes fps*2)
        var debug = Environment.GetEnvironmentVariable("XCFA_DEBUG") == "1";
        if (debug)
        {
            Console.Error.WriteLine($"[xcfa-debug] hasSilenceSegs={hasSilenceSegs} availableMuls=[{string.Join(",", silenceLibrary.AvailableMuls)}] minSilFrames={minSilFrames} silenceThresh={silenceThresh}");
            var longRuns = 0;
            for (var i = 0; i < energies.Length; i++)
                if (energies[i] < silenceThresh && (i == 0 || energies[i - 1] >= silenceThresh))
                {
                    var runLenDbg = 0;
                    for (var j = i; j < energies.Length && energies[j] < silenceThresh; j++) runLenDbg++;
                    if (runLenDbg >= minSilFrames) longRuns++;
                    Console.Error.WriteLine($"[xcfa-debug] silence run start@{i} len={runLenDbg} qualifies={runLenDbg >= minSilFrames}");
                }
            Console.Error.WriteLine($"[xcfa-debug] total qualifying long runs: {longRuns}");
            for (var fi = 0; fi < Math.Min(3, nFrames); fi++)
                Console.Error.WriteLine($"[xcfa-debug] runtime feats[{fi}]=[{string.Join(",", feats[fi].Select(v => v.ToString("F3")))}]");
        }
        var framesPerStep = Math.Max(2, fps / SilEntryMuls.Length);
        var exitFramesPerStep = Math.Max(1, (int)Math.Round(fps * exitSeconds / SilExitMuls.Length));
        var exitTotalFrames = Math.Max(SilExitMuls.Length, (int)Math.Round(fps * exitSeconds)); // total EXIT ramp length in frames, reserved out of the tail of the silence run
        var sustainStepFrames = Math.Max(2, fps / SilSustainMuls.Length);

        var silenceRuns = ComputeSilenceRuns(energies, silenceThresh, nFrames);

        // ---- silence state machine ----
        var silState = "none"; // "none" | "active" | "exit_draining"
        var silQueue = new Queue<Frame>();
        Frame? silCurFrame = null;
        var silEndFrame = -1;
        var silEntryMul = 0.5f;
        var sustainCyclePos = 0;
        var sustainRunEnd = 0;

        // ---- speech state ----
        var compositeBuf = baseFrame.Clone();
        var lastFrame = baseFrame;
        var curSegId = -1;
        var curFrameIdx = 0;
        var cooldownLeft = 0;
        // Stored frames are captured at sampleFps (Extractor.py: frame_step = video_fps/sample_fps,
        // so every stored frame is genuinely 1/sampleFps apart), but _render_frame_level_mode's
        // "cur_frame_idx += 1" fires on EVERY output render frame with no accounting for the
        // fps/sampleFps ratio -- a real pacing bug in the Python original (no interp_per_pair is
        // even passed into that function), reproduced faithfully by the first version of this
        // port and then reported back as "monstrously fast lip movement". Fixed here (a genuine
        // behavior change from the literal Python source, not a straight port) by advancing the
        // stored-frame position at sampleFps/fps per render frame via a fractional accumulator,
        // instead of 1 whole stored frame per render frame.
        var frameIdxAccum = 0.0;
        var storedFramesPerRenderFrame = sampleFps / (double)fps; // e.g. 5/25 = 0.2 stored frames advanced per render frame

        var frameCursor = 0;
        while (frameCursor < nFrames)
        {
            var energy = energies[Math.Min(frameCursor, energies.Length - 1)];
            var inSil = energy < silenceThresh;
            var runLen = silenceRuns[Math.Min(frameCursor, silenceRuns.Length - 1)];

            void EmitAndAdvance(Frame f)
            {
                var outFrame = Composite(compositeBuf, f, faceRegion);
                if (micro)
                    outFrame = ApplyMicro(outFrame, frameCursor / (double)fps, styleIntensity);
                if (useAlphaChannel)
                    writer.WriteFrame(AlphaCompositor.ToBgra(outFrame, baseMask!));
                else
                    writer.WriteFrame(outFrame.Bgr);
                frameCursor += 1;
            }

            // ================================================================
            // DRAIN the silence queue
            // ================================================================
            if (silState == "active")
            {
                if (silQueue.Count > 0)
                {
                    var f = silQueue.Dequeue();
                    silCurFrame = f;
                    lastFrame = f;
                    EmitAndAdvance(f);
                    continue;
                }

                if (frameCursor < sustainRunEnd)
                {
                    var mul = SilSustainMuls[sustainCyclePos % SilSustainMuls.Length];
                    sustainCyclePos += 1;
                    var (stepFrames, finalFrame) = silenceLibrary.BuildPhase(
                        new[] { mul }, sustainStepFrames, silCurFrame ?? lastFrame, faceRegion);
                    foreach (var sf in stepFrames) silQueue.Enqueue(sf);
                    silCurFrame = finalFrame;
                    if (silQueue.Count > 0)
                    {
                        var f = silQueue.Dequeue();
                        silCurFrame = f;
                        lastFrame = f;
                        EmitAndAdvance(f);
                        continue;
                    }
                }
                else
                {
                    var avail = silenceLibrary.AvailableMuls;
                    var maxAvail = avail.Count > 0 ? avail.Max() : 0.5f;
                    var exitTop = Math.Min(silEntryMul, maxAvail);
                    var n = SilExitMuls.Length;
                    var exitMuls = Enumerable.Range(0, n)
                        .Select(i => MathF.Round(exitTop * i / (n - 1), 1)).ToArray();
                    var (exitPhaseFrames, finalFrame) = silenceLibrary.BuildPhase(
                        exitMuls, exitFramesPerStep, silCurFrame ?? lastFrame, faceRegion);
                    foreach (var sf in exitPhaseFrames) silQueue.Enqueue(sf);
                    silCurFrame = finalFrame;
                    silState = "exit_draining";
                    if (silQueue.Count > 0)
                    {
                        var f = silQueue.Dequeue();
                        silCurFrame = f;
                        lastFrame = f;
                        EmitAndAdvance(f);
                        continue;
                    }
                }
            }

            if (silState == "exit_draining")
            {
                if (silQueue.Count > 0)
                {
                    var f = silQueue.Dequeue();
                    silCurFrame = f;
                    lastFrame = f;
                    EmitAndAdvance(f);
                    continue;
                }
                silState = "none";
            }

            // ================================================================
            // Detect START of a long silence run
            // ================================================================
            var prevEnergy = frameCursor > 0 ? energies[Math.Min(frameCursor - 1, energies.Length - 1)] : 1.0f;
            var isRunStart = inSil && prevEnergy >= silenceThresh;

            if (isRunStart && runLen >= minSilFrames && silState == "none" && hasSilenceSegs && frameCursor > silEndFrame)
            {
                var avail = silenceLibrary.AvailableMuls;
                var maxAvail = avail.Count > 0 ? avail.Max() : 0.5f;
                silEntryMul = Math.Min(0.5f, maxAvail);
                var n = SilEntryMuls.Length;
                var entryMuls = Enumerable.Range(0, n)
                    .Select(i => MathF.Round(silEntryMul * (1.0f - (float)i / (n - 1)), 1)).ToArray();
                var (entryFrames, finalFrame) = silenceLibrary.BuildPhase(entryMuls, framesPerStep, lastFrame, faceRegion);
                foreach (var sf in entryFrames) silQueue.Enqueue(sf);
                silCurFrame = finalFrame;

                sustainRunEnd = frameCursor + runLen - exitTotalFrames; // leave room for the EXIT ramp
                silEndFrame = frameCursor + runLen;
                silState = "active";
                sustainCyclePos = 0;

                if (silQueue.Count > 0)
                {
                    var f = silQueue.Dequeue();
                    silCurFrame = f;
                    lastFrame = f;
                    EmitAndAdvance(f);
                    continue;
                }
            }

            // ================================================================
            // Normal speech / short-silence rendering
            // ================================================================
            if (cooldownLeft <= 0)
            {
                var feat = feats[Math.Min(frameCursor, feats.Length - 1)];
                var pos = library.FindBestFrame(feat, curSegId, curFrameIdx, switchThreshold);
                if (debug && pos.SegmentId != curSegId)
                    Console.Error.WriteLine($"[xcfa-debug] frame={frameCursor} JUMP seg {curSegId}->{pos.SegmentId} frameIdx->{pos.FrameIndex} energy={energy:F4}");
                curSegId = pos.SegmentId;
                curFrameIdx = pos.FrameIndex;
                frameIdxAccum = curFrameIdx; // re-sync the fractional accumulator to the decided position
                cooldownLeft = jumpCooldown;
            }
            else
            {
                frameIdxAccum += storedFramesPerRenderFrame;
                var candidateIdx = (int)Math.Floor(frameIdxAccum);
                var next = new FramePosition(curSegId, candidateIdx);
                if (candidateIdx != curFrameIdx && library.HasFrame(next)) curFrameIdx = candidateIdx;
                cooldownLeft -= 1;
            }

            if (curSegId >= 0)
            {
                var decoded = getDecodedSegment(curSegId);
                if (decoded.Length > 0)
                    lastFrame = decoded[Math.Min(curFrameIdx, decoded.Length - 1)];
            }

            EmitAndAdvance(lastFrame);
        }
    }

    /// <summary>
    /// For every frame index, the total length of the silence run it belongs
    /// to (0 if it's speech). Direct port of _compute_silence_runs.
    /// </summary>
    private static int[] ComputeSilenceRuns(float[] energies, float silenceThresh, int nFrames)
    {
        var result = new int[nFrames];
        var i = 0;
        while (i < nFrames)
        {
            if (energies[Math.Min(i, energies.Length - 1)] < silenceThresh)
            {
                var j = i + 1;
                while (j < nFrames && energies[Math.Min(j, energies.Length - 1)] < silenceThresh) j += 1;
                for (var k = i; k < j; k++) result[k] = j - i;
                i = j;
            }
            else
            {
                i += 1;
            }
        }
        return result;
    }

    private static byte[]? LoadSegmentMask(XcfaFile xcfa, XcfaSegment seg, int frameIdx, int outW, int outH, Dictionary<int, byte[]?[]>? maskCache)
    {
        if (maskCache is not null)
        {
            if (!maskCache.TryGetValue(seg.Id, out var arr))
            {
                arr = new byte[]?[seg.Frames.Length];
                maskCache[seg.Id] = arr;
            }
            if (arr[frameIdx] is { } cached) return cached;
            var m = AlphaCompositor.LoadFrameMask(xcfa, seg, frameIdx, outW, outH);
            if (m is not null) arr[frameIdx] = m;
            return m;
        }
        return AlphaCompositor.LoadFrameMask(xcfa, seg, frameIdx, outW, outH);
    }

    /// <summary>Paste the animated face region onto the composite buffer background, or return the animated frame as-is if there's no face region.</summary>
    private static Frame Composite(Frame compositeBuf, Frame animated, (int X, int Y, int W, int H)? region)
    {
        if (region is null) return animated;
        var (x0, y0, w, h) = region.Value;
        for (var y = y0; y < y0 + h && y < compositeBuf.Height; y++)
        {
            if (y < 0) continue;
            for (var x = x0; x < x0 + w && x < compositeBuf.Width; x++)
            {
                if (x < 0) continue;
                var o = (y * compositeBuf.Width + x) * 3;
                compositeBuf.Bgr[o] = animated.Bgr[o];
                compositeBuf.Bgr[o + 1] = animated.Bgr[o + 1];
                compositeBuf.Bgr[o + 2] = animated.Bgr[o + 2];
            }
        }
        return compositeBuf;
    }

    /// <summary>Always starts fresh from baseFrame (no persistent buffer) -- matches _composite's own base_img.copy() every call, unlike frame mode's reused composite_buf.</summary>
    private static Frame ComposeFresh(Frame baseFrame, Frame animated, (int X, int Y, int W, int H)? region)
    {
        if (region is null) return animated.Clone();
        var result = baseFrame.Clone();
        var (x0, y0, w, h) = region.Value;
        for (var y = y0; y < y0 + h && y < result.Height; y++)
        {
            if (y < 0) continue;
            for (var x = x0; x < x0 + w && x < result.Width; x++)
            {
                if (x < 0) continue;
                var o = (y * result.Width + x) * 3;
                result.Bgr[o] = animated.Bgr[o];
                result.Bgr[o + 1] = animated.Bgr[o + 1];
                result.Bgr[o + 2] = animated.Bgr[o + 2];
            }
        }
        return result;
    }

    /// <summary>
    /// Segment-based render: picks whole segments via audio-lookahead +
    /// stitch quality, then plays each one back via intra-segment optical-
    /// flow interpolation (stored flow blobs, not live Farneback), with a
    /// live Farneback morph when cutting between segments. Direct,
    /// line-for-line port of _render_worker.py's _render_segment_mode +
    /// _select_segment. NOT the production default (see Renderer's class
    /// doc) -- available via RenderOptions.Mode = RenderMode.Segment.
    /// </summary>
    private static void RenderSegmentMode(
        Encoder writer, int nFrames, int fps,
        SegmentLibrary library, SilenceLibrary silenceLibrary, AudioFeatures.FeatureStream feats, float[] energies,
        Frame baseFrame, (int X, int Y, int W, int H)? faceRegion, Func<int, Frame[]> getDecodedSegment,
        XcfaFile xcfa, XcfaCatalog catalog, int outW, int outH,
        bool useAlphaChannel, byte[]? baseMask, bool micro, double styleIntensity,
        float silenceThresh, int interpPerPair, int minSegFrames, int silenceOnset, int lookaheadFrames)
    {
        // Real segment Ids of the "default" silence entries (prefer mul=0.0). Direct port of
        // `quiet_seg_ids = {sd[0] for sd in library.silence_data}`.
        var quietSegIds = new HashSet<int>(silenceLibrary.DefaultEntries.Select(e => e.SegId));
        var rng = new Random();

        void Emit(Frame animated, (int X, int Y, int W, int H)? region, int frameCursorForMicro)
        {
            var composed = ComposeFresh(baseFrame, animated, region);
            if (micro) composed = ApplyMicro(composed, frameCursorForMicro / (double)fps, styleIntensity);
            if (useAlphaChannel) writer.WriteFrame(AlphaCompositor.ToBgra(composed, baseMask!));
            else writer.WriteFrame(composed.Bgr);
        }

        var frameCursor = 0;
        var lastSegPos = -1;
        var lastFrame = baseFrame;
        var silenceStreak = 0;

        while (frameCursor < nFrames)
        {
            var energy = energies[Math.Min(frameCursor, energies.Length - 1)];
            var inSilence = energy < silenceThresh;
            silenceStreak = inSilence ? silenceStreak + 1 : 0;

            var segPos = SelectSegment(library, feats, frameCursor, lookaheadFrames, nFrames,
                lastSegPos, silenceStreak, quietSegIds, silenceOnset, rng);
            var segId = library.IdAt(segPos);
            var segMeta = catalog.Segments.First(s => s.Id == segId);
            var segFrames = getDecodedSegment(segId);

            if (segFrames.Length < 2)
            {
                var nFallback = (segMeta.FrameCount - 1) * interpPerPair;
                for (var i = 0; i < nFallback && frameCursor < nFrames; i++)
                {
                    // Bare base-image write -- matches _render_segment_mode's own fallback exactly:
                    // it calls writer.write(base_img) directly here, bypassing _composite/_apply_micro.
                    if (useAlphaChannel) writer.WriteFrame(AlphaCompositor.ToBgra(baseFrame, baseMask!));
                    else writer.WriteFrame(baseFrame.Bgr);
                    frameCursor += 1;
                }
                lastFrame = baseFrame;
                lastSegPos = segPos;
                continue;
            }

            if (lastSegPos >= 0 && frameCursor < nFrames)
            {
                var morphFrames = Transition.ComputeMorphFrames(lastFrame, segFrames[0], faceRegion);
                foreach (var mf in morphFrames)
                {
                    if (frameCursor >= nFrames) break;
                    Emit(mf, faceRegion, frameCursor);
                    frameCursor += 1;
                }
            }

            var flows = SegmentFlows(xcfa, segMeta, outW, outH);

            var lastRendered = segFrames[0];
            var segSilenceCnt = 0;
            var segInterrupted = false;

            for (var pairIdx = 0; pairIdx < segFrames.Length - 1 && !segInterrupted; pairIdx++)
            {
                var fa = segFrames[pairIdx];
                var fb = segFrames[pairIdx + 1];
                var (flowAb, flowBa) = pairIdx < flows.Length ? flows[pairIdx] : flows[^1];

                for (var subIdx = 0; subIdx < interpPerPair; subIdx++)
                {
                    if (frameCursor >= nFrames) { segInterrupted = true; break; }
                    var curEnergy = energies[Math.Min(frameCursor, energies.Length - 1)];
                    if (curEnergy < silenceThresh)
                    {
                        segSilenceCnt += 1;
                        if (segSilenceCnt >= silenceOnset) { segInterrupted = true; break; }
                    }
                    else
                    {
                        segSilenceCnt = 0;
                    }

                    var t = subIdx / (float)interpPerPair;
                    var interp = Warp.Interpolate(fa, fb, flowAb, flowBa, t);
                    lastRendered = interp;
                    Emit(interp, faceRegion, frameCursor);
                    frameCursor += 1;
                }
            }

            lastFrame = lastRendered;
            lastSegPos = segPos;
            if (segInterrupted && segSilenceCnt >= silenceOnset) silenceStreak = silenceOnset;

            if (!segInterrupted)
            {
                var segComposed = ComposeFresh(baseFrame, lastRendered, faceRegion);
                var framesThisSeg = interpPerPair * (segFrames.Length - 1);
                while (framesThisSeg < minSegFrames && frameCursor < nFrames)
                {
                    var curEnergy = energies[Math.Min(frameCursor, energies.Length - 1)];
                    if (curEnergy < silenceThresh)
                    {
                        silenceStreak += 1;
                        if (silenceStreak >= silenceOnset) break;
                    }
                    else
                    {
                        silenceStreak = 0;
                    }
                    var hold = segComposed;
                    if (micro) hold = ApplyMicro(hold, frameCursor / (double)fps, styleIntensity);
                    if (useAlphaChannel) writer.WriteFrame(AlphaCompositor.ToBgra(hold, baseMask!));
                    else writer.WriteFrame(hold.Bgr);
                    frameCursor += 1;
                    framesThisSeg += 1;
                }
            }
        }
    }

    /// <summary>
    /// Picks the next segment POSITION using lookahead audio + stitch
    /// quality. Direct port of _select_segment -- including its apparent
    /// position/real-id mixing when falling back to quiet segments (see
    /// SegmentLibrary's class doc): quietSegIds holds real catalog Ids, and
    /// exactly like the Python original, those values get used directly as
    /// segment POSITIONS in the fallback branches below. This only matters
    /// when a model's segment ids don't equal their position in
    /// catalog.Segments; Packager.py assigns ids sequentially, so in
    /// practice the two coincide for every real model.
    /// </summary>
    private static int SelectSegment(SegmentLibrary library, AudioFeatures.FeatureStream feats, int frameCursor,
        int lookaheadFrames, int nFrames, int lastSegPos, int silenceStreak,
        HashSet<int> quietSegIds, int silenceOnset, Random rng)
    {
        var end = Math.Min(nFrames, frameCursor + lookaheadFrames);
        // AudioFeatures.Compute always returns a real (EMA-smoothed) vector per frame -- there is
        // no "None" entry in this port (that's only possible from Features.py's standalone
        // computeAudioFeatures, which the actual render loop's all_feats never uses; see
        // AudioFeatures.cs's header doc). So the "if not valid" branch below is dead code here,
        // exactly as it is dead in production for the same reason.
        if (frameCursor >= end)
        {
            if (quietSegIds.Count > 0) return library.BestStitch(quietSegIds.ToList(), lastSegPos);
            return library.RandomBlinkSegment(rng);
        }

        var aheadMean = new float[16];
        var n = end - frameCursor;
        for (var i = frameCursor; i < end; i++)
            for (var d = 0; d < 16; d++)
                aheadMean[d] += feats[i][d];
        for (var d = 0; d < 16; d++) aheadMean[d] /= n;

        var top20 = library.CosineTopK(aheadMean, 20, exclude: lastSegPos);
        if (top20.Count == 0) top20 = library.CosineTopK(aheadMean, 20, exclude: -1);

        if (silenceStreak >= silenceOnset && quietSegIds.Count > 0)
        {
            var quietCands = top20.Where(c => quietSegIds.Contains(c)).ToList();
            top20 = quietCands.Count > 0 ? quietCands : quietSegIds.ToList();
        }

        return library.BestStitch(top20, lastSegPos);
    }

    /// <summary>
    /// Full-resolution (Ab, Ba) flow pairs for every consecutive frame pair
    /// in a segment, decoded from the .xcfa's stored int8 blobs. Direct port
    /// of _get_segment_flows -- xcfa always has stored flow blobs (unlike
    /// legacy .cfa, which could fall back to live Farneback here), so the
    /// live-compute fallback branch is not applicable to this xcfa-only
    /// library.
    /// </summary>
    private static (float[,,] Ab, float[,,] Ba)[] SegmentFlows(XcfaFile xcfa, XcfaSegment seg, int outW, int outH)
    {
        var result = new (float[,,], float[,,])[seg.Flows.Length];
        for (var i = 0; i < seg.Flows.Length; i++)
        {
            var (fwdRaw, bwdRaw, scale) = xcfa.ReadFlowPair(seg, i);
            result[i] = (Warp.UpsampleFlow(fwdRaw, scale, outW, outH), Warp.UpsampleFlow(bwdRaw, scale, outW, outH));
        }
        return result;
    }

    /// <summary>Subtle sinusoidal translation to simulate micro head movement. Direct port of _apply_micro.</summary>
    private static Frame ApplyMicro(Frame frame, double tSeconds, double amp)
    {
        if (amp < 0.1) return frame;
        var dx = amp * Math.Sin(tSeconds * 0.7);
        var dy = amp * Math.Cos(tSeconds * 0.5);
        if (Math.Abs(dx) < 0.5 && Math.Abs(dy) < 0.5) return frame;
        return Warp.Translate(frame, (float)dx, (float)dy);
    }

    private static (int W, int H) ResolveOutputSize(int baseW, int baseH, RenderOptions options)
    {
        // Named preset resolves to a target height, but only applies when neither Width nor
        // Height was given explicitly -- matches main()'s "if resolution and not width and not height".
        int? height = options.Height;
        if (options.Resolution is { } resPreset && options.Width is null && options.Height is null
            && ResolutionPresets.Values.TryGetValue(resPreset, out var presetH))
        {
            height = presetH;
        }

        int outW, outH;
        if (options.Width is { } w0 && height is { } h0)
        {
            outW = w0; outH = h0;
        }
        else if (options.Width is { } w1)
        {
            outW = w1;
            outH = (int)Math.Round(baseH * (double)w1 / baseW);
        }
        else if (height is { } h1)
        {
            outH = h1;
            outW = (int)Math.Round(baseW * (double)h1 / baseH);
        }
        else if (options.Scale != 1.0)
        {
            outW = (int)Math.Round(baseW * options.Scale);
            outH = (int)Math.Round(baseH * options.Scale);
        }
        else
        {
            outW = baseW; outH = baseH;
        }
        outW += outW % 2; // most video codecs require even dimensions
        outH += outH % 2;
        return (outW, outH);
    }

    private static Frame ResizeFrame(Frame frame, int w, int h)
    {
        // Re-encode-free resize: draw through SkiaSharp via a JPEG round-trip would
        // be wasteful for a single base-image resize; reuse Warp's bilinear sampler
        // by treating it as a per-axis scale (nearest source mapping matches
        // cv2.resize's INTER_LINEAR closely enough for a single base composite).
        var result = new Frame(w, h);
        for (var y = 0; y < h; y++)
        {
            var sy = (y + 0.5f) * frame.Height / h - 0.5f;
            for (var x = 0; x < w; x++)
            {
                var sx = (x + 0.5f) * frame.Width / w - 0.5f;
                SampleInto(frame, sx, sy, result, x, y);
            }
        }
        return result;
    }

    private static void SampleInto(Frame src, float sx, float sy, Frame dst, int dx, int dy)
    {
        var w = src.Width;
        var h = src.Height;
        var x0 = (int)Math.Floor(sx);
        var y0 = (int)Math.Floor(sy);
        var fx = sx - x0;
        var fy = sy - y0;
        var x0c = Math.Clamp(x0, 0, w - 1);
        var x1c = Math.Clamp(x0 + 1, 0, w - 1);
        var y0c = Math.Clamp(y0, 0, h - 1);
        var y1c = Math.Clamp(y0 + 1, 0, h - 1);
        var o = (dy * dst.Width + dx) * 3;
        for (var ch = 0; ch < 3; ch++)
        {
            var v00 = src.Bgr[(y0c * w + x0c) * 3 + ch];
            var v01 = src.Bgr[(y0c * w + x1c) * 3 + ch];
            var v10 = src.Bgr[(y1c * w + x0c) * 3 + ch];
            var v11 = src.Bgr[(y1c * w + x1c) * 3 + ch];
            var top = v00 + (v01 - v00) * fx;
            var bot = v10 + (v11 - v10) * fx;
            var v = top + (bot - top) * fy;
            dst.Bgr[o + ch] = (byte)Math.Clamp(v, 0, 255);
        }
    }

    private static readonly Dictionary<string, (byte B, byte G, byte R)> NamedColors = new(StringComparer.OrdinalIgnoreCase)
    {
        ["black"] = (0, 0, 0),
        ["white"] = (255, 255, 255),
        ["green"] = (0, 255, 0),
        ["blue"] = (255, 0, 0),
        ["red"] = (0, 0, 255),
        ["gray"] = (128, 128, 128),
        ["grey"] = (128, 128, 128),
    };

    /// <summary>Parses a CSS-style color name or #RGB/#RRGGBB hex string to (B, G, R). Direct port of _parse_bg_color_bgr.</summary>
    private static (byte B, byte G, byte R) ParseBgColorBgr(string color)
    {
        if (NamedColors.TryGetValue(color, out var named)) return named;

        var hex = color.TrimStart('#');
        if (hex.Length == 3)
            hex = string.Concat(hex.Select(c => new string(c, 2)));
        if (hex.Length == 6
            && byte.TryParse(hex[..2], System.Globalization.NumberStyles.HexNumber, null, out var r)
            && byte.TryParse(hex[2..4], System.Globalization.NumberStyles.HexNumber, null, out var g)
            && byte.TryParse(hex[4..6], System.Globalization.NumberStyles.HexNumber, null, out var b))
        {
            return (b, g, r);
        }

        throw new ArgumentException($"Unrecognized bg color: \"{color}\" (expected a CSS name or #RGB/#RRGGBB hex).");
    }
}

namespace XcfaRenderer;

/// <summary>
/// Decoded silence-tagged segments, grouped by silence_mul (0.0..1.0),
/// backing the living-pause 3-phase state machine in Renderer.cs
/// (ENTRY/SUSTAIN/EXIT) -- direct port of _render_worker.py's
/// precompute_silence_flows (both the new-model tagged-segment branch AND
/// the old-model energy-based fallback for models with no silence_mul tags
/// at all -- ported regardless of container format, since that fallback is
/// about model content, not about the legacy .cfa file format) plus
/// _pick_silence_seg and _nearest_mul.
/// </summary>
public sealed class SilenceLibrary
{
    private const int MaxPerMul = 5;

    public IReadOnlyDictionary<float, List<(int SegId, Frame[] Frames)>> ByMul { get; }
    public IReadOnlyList<float> AvailableMuls { get; }
    public bool HasData => ByMul.Count > 0;

    /// <summary>
    /// The flattened "default" entry list _SegmentLibrary.silence_data
    /// exposes: prefer mul=0.0's entries, else fall back to the lowest
    /// available multiplier's. Used by render_mode="segment" for its
    /// quiet_seg_ids set and silent-passage segment fallback.
    /// </summary>
    public IReadOnlyList<(int SegId, Frame[] Frames)> DefaultEntries { get; }

    private SilenceLibrary(Dictionary<float, List<(int, Frame[])>> byMul)
    {
        ByMul = byMul;
        AvailableMuls = byMul.Keys.OrderBy(m => m).ToArray();
        if (byMul.TryGetValue(0.0f, out var zero)) DefaultEntries = zero;
        else if (AvailableMuls.Count > 0) DefaultEntries = byMul[AvailableMuls[0]];
        else DefaultEntries = Array.Empty<(int, Frame[])>();
    }

    /// <summary>
    /// applyBg, if given, is called on every decoded frame right after decode
    /// (segId, frameIdx, frame) -> processed frame -- lets Renderer bake in
    /// color-fill/blacken background handling once, up front, the same way
    /// production's _apply_bg_to_frames runs "at first cache fill" (see
    /// XcfaBgUpgrader.py/_apply_bg_to_frames doc). Not needed for the WebM
    /// alpha-channel case: RGB there stays untouched and the base mask is
    /// used for silence-phase frames' alpha (see Renderer.cs).
    /// </summary>
    public static SilenceLibrary Build(XcfaFile xcfa, int outW, int outH,
        Func<int, int, Frame, Frame>? applyBg = null)
    {
        var tagged = new Dictionary<float, List<XcfaSegment>>();
        foreach (var seg in xcfa.Catalog.Segments)
        {
            if (!seg.IsSilence) continue;
            var mul = MathF.Round(seg.SilenceMul, 1);
            if (!tagged.TryGetValue(mul, out var list)) { list = new List<XcfaSegment>(); tagged[mul] = list; }
            list.Add(seg);
        }

        var byMul = new Dictionary<float, List<(int, Frame[])>>();
        foreach (var (mul, segs) in tagged)
        {
            var entries = new List<(int, Frame[])>();
            foreach (var seg in segs.Take(MaxPerMul))
            {
                if (seg.Frames.Length < 2) continue;
                var frames = new Frame[seg.Frames.Length];
                for (var i = 0; i < seg.Frames.Length; i++)
                {
                    var f = Frame.DecodeJpeg(xcfa.ReadFrame(seg, i), outW, outH);
                    frames[i] = applyBg is null ? f : applyBg(seg.Id, i, f);
                }
                entries.Add((seg.Id, frames));
            }
            if (entries.Count > 0) byMul[mul] = entries;
        }

        if (byMul.Count == 0)
        {
            // ---- Fallback: energy-based quiet segments (models with no silence_mul tags at all). ----
            // Direct port of precompute_silence_flows's old-model branch: rank every
            // segment by its mean_features[13] (the RMS-energy dimension -- see
            // AudioFeatures.Compute's 16-dim layout: index 13 is energy), take the
            // quietest max(3, min(10, n_segments/8)), decode those.
            var energyRanked = xcfa.Catalog.Segments
                .Select(s => (Energy: s.MeanFeatures.Length > 13 ? s.MeanFeatures[13] : 1f, Seg: s))
                .OrderBy(t => t.Energy)
                .ToList();
            var nQuiet = Math.Max(3, Math.Min(10, Math.Max(1, energyRanked.Count) / 8));

            var fallback = new List<(int, Frame[])>();
            foreach (var (_, seg) in energyRanked.Take(nQuiet))
            {
                if (seg.Frames.Length < 2) continue;
                var frames = new Frame[seg.Frames.Length];
                for (var i = 0; i < seg.Frames.Length; i++)
                {
                    var f = Frame.DecodeJpeg(xcfa.ReadFrame(seg, i), outW, outH);
                    frames[i] = applyBg is null ? f : applyBg(seg.Id, i, f);
                }
                fallback.Add((seg.Id, frames));
            }

            if (fallback.Count == 0 && xcfa.Catalog.Segments.Length > 0)
            {
                var seg0 = xcfa.Catalog.Segments[0];
                if (seg0.Frames.Length >= 2)
                {
                    var frames = new Frame[seg0.Frames.Length];
                    for (var i = 0; i < seg0.Frames.Length; i++)
                    {
                        var f = Frame.DecodeJpeg(xcfa.ReadFrame(seg0, i), outW, outH);
                        frames[i] = applyBg is null ? f : applyBg(seg0.Id, i, f);
                    }
                    fallback.Add((seg0.Id, frames));
                }
            }

            if (fallback.Count > 0) byMul[0.0f] = fallback;
        }

        return new SilenceLibrary(byMul);
    }

    /// <summary>Nearest available multiplier to target, or null if no silence data exists at all. Direct port of _nearest_mul.</summary>
    public float? NearestMul(float target)
    {
        if (AvailableMuls.Count == 0) return null;
        return AvailableMuls.OrderBy(m => Math.Abs(m - target)).First();
    }

    /// <summary>
    /// Among the entries for the multiplier nearest to targetMul, picks the
    /// one whose first frame best stitches (smallest mean-abs-diff in the
    /// face ROI) to lastFrame. Direct port of _pick_silence_seg.
    /// </summary>
    public (int SegId, Frame[] Frames)? PickSegment(float targetMul, Frame? lastFrame, (int X, int Y, int W, int H)? region)
    {
        var mul = NearestMul(targetMul);
        if (mul is null || !ByMul.TryGetValue(mul.Value, out var entries) || entries.Count == 0)
            return null;
        if (entries.Count == 1 || lastFrame is null)
            return entries[0];

        var best = entries[0];
        var bestMad = double.MaxValue;
        foreach (var e in entries)
        {
            var mad = MeanAbsDiff(e.Frames[0], lastFrame, region);
            if (mad < bestMad) { bestMad = mad; best = e; }
        }
        return best;
    }

    private static double MeanAbsDiff(Frame a, Frame b, (int X, int Y, int W, int H)? region)
    {
        var (x0, y0, w, h) = region is { } r ? r : (0, 0, a.Width, a.Height);
        double sum = 0;
        long count = 0;
        for (var y = y0; y < y0 + h; y++)
            for (var x = x0; x < x0 + w; x++)
            {
                var o = (y * a.Width + x) * 3;
                for (var c = 0; c < 3; c++)
                {
                    sum += Math.Abs(a.Bgr[o + c] - b.Bgr[o + c]);
                    count++;
                }
            }
        return count == 0 ? 0 : sum / count;
    }

    /// <summary>
    /// Builds one silence phase (entry, one sustain step, or exit): for each
    /// step multiplier, live-morphs from the current frame into the picked
    /// segment's first frame, then plays framesPerStep frames cycling
    /// through that segment. Direct port of _build_silence_phase (the morph
    /// step is exactly what Transition.cs exists for -- this is its one
    /// real caller in this library).
    /// </summary>
    public (List<Frame> Frames, Frame FinalFrame) BuildPhase(
        IReadOnlyList<float> mulSteps, int framesPerStep, Frame lastFrame, (int X, int Y, int W, int H)? region)
    {
        var outFrames = new List<Frame>();
        var cur = lastFrame;

        foreach (var mul in mulSteps)
        {
            var entry = PickSegment(mul, cur, region);
            if (entry is null)
            {
                for (var i = 0; i < framesPerStep; i++) outFrames.Add(cur);
                continue;
            }

            var (_, segFrames) = entry.Value;
            var morph = Transition.ComputeMorphFrames(cur, segFrames[0], region);
            outFrames.AddRange(morph);

            for (var i = 0; i < framesPerStep; i++)
                outFrames.Add(segFrames[i % segFrames.Length]);
            cur = outFrames[^1];
        }

        return (outFrames, cur);
    }
}

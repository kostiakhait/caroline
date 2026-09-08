namespace XcfaRenderer;

/// <summary>Identifies one stored frame inside the library: which segment, which frame index within it.</summary>
public readonly record struct FramePosition(int SegmentId, int FrameIndex);

/// <summary>
/// Per-frame audio-feature index over every stored segment frame, used for
/// cosine-similarity phoneme matching with hysteresis, plus the whole-segment
/// selection machinery render_mode="segment" uses. Direct port of
/// _render_worker.py's _SegmentLibrary.
///
/// IMPORTANT indexing note carried over from the Python original: the
/// frame-level members (FindBestFrame/HasFrame) key by a segment's real
/// catalog Id (matches _SegmentLibrary.seg_frame_idx, which is built from
/// seg["id"]). The whole-segment members (CosineTopK/BestStitch/
/// RandomBlinkSegment, and _select_segment's "seg_id" in Python) instead
/// operate on the segment's POSITION in catalog.Segments -- exactly
/// mirroring self.feat_norms/self.first_thumbs/self.segments being plain
/// Python lists indexed 0..N-1, NOT dicts keyed by seg["id"]. The two only
/// coincide if a model's segments happen to be numbered 0..N-1 in catalog
/// order, which isn't guaranteed. Callers needing the real Id for a
/// position must read catalog.Segments[position].Id themselves (see
/// Renderer.cs's segment-mode path).
/// </summary>
public sealed class SegmentLibrary
{
    private readonly XcfaCatalog _catalog;
    private readonly float[][] _normalizedFeatMatrix; // (N_valid, 16), each row L2-normalized
    private readonly FramePosition[] _registry;        // row i -> (segId, frameIdx)
    private readonly Dictionary<FramePosition, int> _index; // (segId, frameIdx) -> row i

    // ---- whole-segment members, indexed by POSITION in catalog.Segments (see class doc) ----
    private readonly float[]?[] _meanFeatNorms; // normalized mean_features per position, or null if absent
    private readonly bool[] _hasBlink;

    public int ValidFrameCount => _registry.Length;
    public int SegmentCount => _catalog.Segments.Length;

    public SegmentLibrary(XcfaCatalog catalog)
    {
        _catalog = catalog;
        var feats = new List<float[]>();
        var registry = new List<FramePosition>();

        foreach (var seg in catalog.Segments)
        {
            for (var fi = 0; fi < seg.AudioFeatures.Length; fi++)
            {
                var raw = seg.AudioFeatures[fi];
                if (raw.All(v => v == 0f)) continue; // all-zero row = "no feature" (see XcfaFile's decode note)
                feats.Add(Normalize(raw));
                registry.Add(new FramePosition(seg.Id, fi));
            }
        }

        _normalizedFeatMatrix = feats.ToArray();
        _registry = registry.ToArray();
        _index = new Dictionary<FramePosition, int>(_registry.Length);
        for (var i = 0; i < _registry.Length; i++)
            _index[_registry[i]] = i;

        _meanFeatNorms = catalog.Segments
            .Select(s => s.MeanFeatures.Length > 0 ? Normalize(s.MeanFeatures) : null)
            .ToArray();
        _hasBlink = catalog.Segments.Select(s => s.Blink).ToArray();
    }

    private static float[] Normalize(float[] v)
    {
        double sumSq = 0;
        foreach (var x in v) sumSq += (double)x * x;
        var norm = (float)Math.Sqrt(sumSq) + 1e-8f;
        var result = new float[v.Length];
        for (var i = 0; i < v.Length; i++)
            result[i] = v[i] / norm;
        return result;
    }

    /// <summary>True if (segmentId, frameIndex) has an indexed feature row -- used for the "does a natural next frame exist" hysteresis check.</summary>
    public bool HasFrame(FramePosition pos) => _index.ContainsKey(pos);

    /// <summary>
    /// Per-frame selection with hysteresis: default behaviour advances to
    /// (curSegId, curFrameIdx+1) for natural sequential playback; jumps to
    /// the globally best-matching stored frame only when it beats the
    /// sequential continuation by more than switchThreshold in cosine
    /// similarity. Direct port of _SegmentLibrary.find_best_frame.
    /// </summary>
    public FramePosition FindBestFrame(float[] feat, int curSegId, int curFrameIdx, float switchThreshold = 0.12f)
    {
        if (_normalizedFeatMatrix.Length == 0)
            return new FramePosition(Math.Max(0, curSegId), curFrameIdx);

        var q = Normalize(feat);

        var bestRow = 0;
        var bestSim = float.NegativeInfinity;
        for (var i = 0; i < _normalizedFeatMatrix.Length; i++)
        {
            var sim = Dot(_normalizedFeatMatrix[i], q);
            if (sim > bestSim)
            {
                bestSim = sim;
                bestRow = i;
            }
        }

        var nextKey = new FramePosition(curSegId, curFrameIdx + 1);
        if (_index.TryGetValue(nextKey, out var nextRow))
        {
            var nextSim = Dot(_normalizedFeatMatrix[nextRow], q);
            if (Environment.GetEnvironmentVariable("XCFA_DEBUG") == "1")
                Console.Error.WriteLine($"[xcfa-debug] bestSim={bestSim:F4} nextSim={nextSim:F4} diff={bestSim - nextSim:F4} stay={bestSim <= nextSim + switchThreshold}");
            if (bestSim <= nextSim + switchThreshold)
                return nextKey; // stay sequential -- not worth jumping
        }
        else if (Environment.GetEnvironmentVariable("XCFA_DEBUG") == "1")
        {
            Console.Error.WriteLine($"[xcfa-debug] bestSim={bestSim:F4} no next-frame candidate (curSeg={curSegId} curIdx={curFrameIdx})");
        }

        return _registry[bestRow];
    }

    /// <summary>
    /// Like FindBestFrame but restricted to quiet-segment frames (quietFrameMask
    /// marks which rows of the flat per-frame matrix belong to a quiet
    /// segment). Falls back to plain FindBestFrame when no quiet frames are
    /// available. Direct port of _find_best_quiet_frame -- note that in the
    /// Python original this function is defined but never actually called
    /// from either render mode (grepped: zero call sites besides its own
    /// def), so this is genuinely dead code upstream too; ported anyway
    /// verbatim rather than dropped.
    /// </summary>
    public FramePosition FindBestQuietFrame(float[]? feat, bool[]? quietFrameMask, ISet<int> quietSegIds,
        int curSegId, int curFrameIdx, float switchThreshold)
    {
        if (feat is null || quietFrameMask is null || !quietFrameMask.Any(b => b))
            return FindBestFrame(feat ?? Array.Empty<float>(), curSegId, curFrameIdx, switchThreshold);

        var q = Normalize(feat);
        var bestRow = -1;
        var bestSim = float.NegativeInfinity;
        for (var i = 0; i < _normalizedFeatMatrix.Length; i++)
        {
            if (!quietFrameMask[i]) continue;
            var sim = Dot(_normalizedFeatMatrix[i], q);
            if (sim > bestSim) { bestSim = sim; bestRow = i; }
        }
        if (bestRow < 0)
            return FindBestFrame(feat, curSegId, curFrameIdx, switchThreshold);

        var nextKey = new FramePosition(curSegId, curFrameIdx + 1);
        if (quietSegIds.Contains(curSegId) && _index.TryGetValue(nextKey, out var nextRow) && quietFrameMask[nextRow])
        {
            var nextSim = Dot(_normalizedFeatMatrix[nextRow], q);
            if (bestSim <= nextSim + switchThreshold)
                return nextKey;
        }

        return _registry[bestRow];
    }

    /// <summary>
    /// Up to k segment POSITIONS sorted by cosine similarity of their
    /// mean_features to queryFeat (best first), excluding `exclude`. Direct
    /// port of _SegmentLibrary.cosine_top_k.
    /// </summary>
    public List<int> CosineTopK(float[] queryFeat, int k = 20, int exclude = -1)
    {
        var q = Normalize(queryFeat);
        var scores = new List<(float Sim, int Pos)>();
        for (var i = 0; i < _meanFeatNorms.Length; i++)
        {
            if (i == exclude || _meanFeatNorms[i] is null) continue;
            scores.Add((Dot(_meanFeatNorms[i]!, q), i));
        }
        scores.Sort((a, b) => b.Sim.CompareTo(a.Sim));
        return scores.Take(k).Select(s => s.Pos).ToList();
    }

    /// <summary>
    /// Among candidate POSITIONS, the one with the best stitch quality
    /// (smallest mean-abs-diff between its first-frame thumbnail and
    /// lastSegPos's last-frame thumbnail) to lastSegPos. Direct port of
    /// _SegmentLibrary.best_stitch.
    ///
    /// XcfaLoader.py's loadModel hardcodes first_thumb_b64/last_thumb_b64 to
    /// "" for every segment of an .xcfa model ("thumbnails not stored in
    /// xcfa -- empty strings keep _SegmentLibrary happy", XcfaLoader.py:117)
    /// -- so for every model this library can open, thumbnails are always
    /// unavailable and this always takes the "last_thumb is None" early
    /// return below, exactly like the Python original does for the same
    /// models. This is a fact about the .xcfa format, not a simplification
    /// made here.
    /// </summary>
    public int BestStitch(IReadOnlyList<int> candidates, int lastSegPos, float[]?[]? firstThumbs = null, float[]?[]? lastThumbs = null)
    {
        var lastThumb = lastThumbs is not null && lastSegPos >= 0 ? lastThumbs[lastSegPos] : null;
        if (lastThumb is null) return candidates[0];

        var bestId = candidates[0];
        var bestDiff = double.MaxValue;
        foreach (var cid in candidates)
        {
            var ft = firstThumbs?[cid];
            if (ft is null) continue;
            double sum = 0;
            for (var i = 0; i < ft.Length; i++) sum += Math.Abs(ft[i] - lastThumb[i]);
            var diff = sum / ft.Length;
            if (diff < bestDiff) { bestDiff = diff; bestId = cid; }
        }
        return bestId;
    }

    /// <summary>A random segment POSITION, preferring ones with Blink=true. Direct port of random_blink_segment.</summary>
    public int RandomBlinkSegment(Random rng)
    {
        var blinkPositions = Enumerable.Range(0, _hasBlink.Length).Where(i => _hasBlink[i]).ToList();
        return blinkPositions.Count > 0 ? blinkPositions[rng.Next(blinkPositions.Count)] : rng.Next(SegmentCount);
    }

    /// <summary>The real catalog segment Id at the given position (see class doc for the position-vs-id distinction).</summary>
    public int IdAt(int position) => _catalog.Segments[position].Id;

    private static float Dot(float[] a, float[] b)
    {
        float sum = 0;
        for (var i = 0; i < a.Length; i++)
            sum += a[i] * b[i];
        return sum;
    }
}

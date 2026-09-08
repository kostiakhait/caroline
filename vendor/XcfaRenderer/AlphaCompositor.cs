namespace XcfaRenderer;

/// <summary>
/// Applies pre-baked alpha masks (see XcfaFile.cs's header doc: masks are
/// baked once, offline, by XcfaBgUpgrader.py's rembg pass -- nothing here
/// touches an ML model). Direct port of _render_worker.py's
/// _apply_bg_to_frames, restricted to the .xcfa path (bg_frames/base_bg are
/// already fully decoded PNG bytes in the catalog -- no lazy per-segment ZIP
/// cache is needed the way the legacy .cfa format requires).
///
/// For the WebM+alpha-channel case, Renderer.cs matches
/// _FfmpegAlphaPipeWriter (_render_worker.py:2363) exactly: every output
/// frame's alpha channel comes from the SAME base-image mask, regardless of
/// which segment frame is showing (ToBgra below is what applies it) --
/// per-frame bg_frames masks are only used for the color-fill/blacken (non-
/// alpha) paths, matching _apply_bg_to_frames. An earlier version of this
/// file used per-frame masks for the alpha channel too (arguably more
/// accurate) -- reverted in favor of matching production bit-for-bit.
/// </summary>
public static class AlphaCompositor
{
    /// <summary>Blends frame with a solid fill color outside the masked (foreground) region. mask: 0=background, 255=foreground.</summary>
    public static void ApplyColorFill(Frame frame, byte[] mask, (byte B, byte G, byte R) fill)
    {
        for (var i = 0; i < mask.Length; i++)
        {
            var alpha = mask[i] / 255f;
            var o = i * 3;
            frame.Bgr[o] = (byte)Math.Clamp(frame.Bgr[o] * alpha + fill.B * (1 - alpha), 0, 255);
            frame.Bgr[o + 1] = (byte)Math.Clamp(frame.Bgr[o + 1] * alpha + fill.G * (1 - alpha), 0, 255);
            frame.Bgr[o + 2] = (byte)Math.Clamp(frame.Bgr[o + 2] * alpha + fill.R * (1 - alpha), 0, 255);
        }
    }

    /// <summary>Blackens the background region (used for non-WebM "remove background, no alpha channel available" output).</summary>
    public static void ApplyBlacken(Frame frame, byte[] mask)
    {
        for (var i = 0; i < mask.Length; i++)
        {
            var alpha = mask[i] / 255f;
            var o = i * 3;
            frame.Bgr[o] = (byte)Math.Clamp(frame.Bgr[o] * alpha, 0, 255);
            frame.Bgr[o + 1] = (byte)Math.Clamp(frame.Bgr[o + 1] * alpha, 0, 255);
            frame.Bgr[o + 2] = (byte)Math.Clamp(frame.Bgr[o + 2] * alpha, 0, 255);
        }
    }

    /// <summary>Builds a BGRA buffer (frame's own RGB untouched, mask supplies the alpha channel) for WebM VP9+alpha output.</summary>
    public static byte[] ToBgra(Frame frame, byte[] mask)
    {
        var n = frame.Width * frame.Height;
        var bgra = new byte[n * 4];
        for (var i = 0; i < n; i++)
        {
            var src = i * 3;
            var dst = i * 4;
            bgra[dst] = frame.Bgr[src];
            bgra[dst + 1] = frame.Bgr[src + 1];
            bgra[dst + 2] = frame.Bgr[src + 2];
            bgra[dst + 3] = mask[i];
        }
        return bgra;
    }

    /// <summary>Decodes the base image's baked mask (only valid when catalog.HasBg).</summary>
    public static byte[] LoadBaseMask(XcfaFile file, int outW, int outH) =>
        Frame.DecodeMaskPng(file.ReadBaseBgMask(), outW, outH);

    /// <summary>Decodes one segment frame's baked mask, or null if this segment has no masks.</summary>
    public static byte[]? LoadFrameMask(XcfaFile file, XcfaSegment segment, int frameIndex, int outW, int outH)
    {
        if (segment.BgFrames is null || frameIndex >= segment.BgFrames.Length) return null;
        return Frame.DecodeMaskPng(file.ReadFrameBgMask(segment, frameIndex), outW, outH);
    }
}

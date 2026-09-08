namespace XcfaRenderer;

/// <summary>
/// Per-pixel optical-flow warp, bidirectional blend, and unsharp compensation
/// -- direct port of _render_worker.py's _warp/_interpolate/_unsharp, used
/// for the intra-segment interpolation and silence-phase animation building
/// blocks a future segment-mode/silence-machine port would need (see
/// SegmentLibrary.cs's header comment: the production default "frame" render
/// mode this library's Renderer.cs replicates does NOT call these -- it
/// plays stored frames back directly with no live warp). Kept as a tested,
/// self-contained unit so that work is not lost.
/// </summary>
public static class Warp
{
    /// <summary>
    /// Bilinear-resizes a raw (fh8, fw8, 2) int8 flow grid (channel 0 = dx,
    /// channel 1 = dy) up to (outH, outW, 2) real-pixel float displacements,
    /// matching XcfaLoader.py's decodeFlowPair + _upsample_flow: multiply by
    /// Scale first, resize, then rescale displacement magnitude by the
    /// width/height ratio.
    /// </summary>
    public static float[,,] UpsampleFlow(sbyte[,,] raw, float scale, int outW, int outH)
    {
        var fh8 = raw.GetLength(0);
        var fw8 = raw.GetLength(1);
        var result = new float[outH, outW, 2];

        if (fh8 == outH && fw8 == outW)
        {
            for (var y = 0; y < outH; y++)
                for (var x = 0; x < outW; x++)
                {
                    result[y, x, 0] = raw[y, x, 0] * scale;
                    result[y, x, 1] = raw[y, x, 1] * scale;
                }
            return result;
        }

        var scaleX = (float)outW / fw8;
        var scaleY = (float)outH / fh8;

        for (var y = 0; y < outH; y++)
        {
            // cv2.resize (INTER_LINEAR) sampling convention: source coord = (dst + 0.5) * (srcSize / dstSize) - 0.5
            var sy = (y + 0.5f) * fh8 / outH - 0.5f;
            var y0 = (int)Math.Floor(sy);
            var fy = sy - y0;
            var y0c = Math.Clamp(y0, 0, fh8 - 1);
            var y1c = Math.Clamp(y0 + 1, 0, fh8 - 1);

            for (var x = 0; x < outW; x++)
            {
                var sx = (x + 0.5f) * fw8 / outW - 0.5f;
                var x0 = (int)Math.Floor(sx);
                var fx = sx - x0;
                var x0c = Math.Clamp(x0, 0, fw8 - 1);
                var x1c = Math.Clamp(x0 + 1, 0, fw8 - 1);

                for (var c = 0; c < 2; c++)
                {
                    var v00 = raw[y0c, x0c, c] * scale;
                    var v01 = raw[y0c, x1c, c] * scale;
                    var v10 = raw[y1c, x0c, c] * scale;
                    var v11 = raw[y1c, x1c, c] * scale;
                    var top = v00 + (v01 - v00) * fx;
                    var bot = v10 + (v11 - v10) * fx;
                    result[y, x, c] = top + (bot - top) * fy;
                }

                result[y, x, 0] *= scaleX;
                result[y, x, 1] *= scaleY;
            }
        }
        return result;
    }

    /// <summary>Translates a whole frame by a constant (dx, dy), bilinear sample, edge-replicate border -- used for the micro-movement effect (see Renderer.cs).</summary>
    public static Frame Translate(Frame frame, float dx, float dy)
    {
        var w = frame.Width;
        var h = frame.Height;
        var result = new Frame(w, h);
        for (var y = 0; y < h; y++)
            for (var x = 0; x < w; x++)
                SampleBilinearReplicate(frame, x - dx, y - dy, result, x, y);
        return result;
    }

    /// <summary>Warps a frame by a full-resolution (H, W, 2) flow field (channel 0 = dx, channel 1 = dy), bilinear sample, edge-replicate border.</summary>
    public static Frame Apply(Frame frame, float[,,] flow, float t = 1f)
    {
        var w = frame.Width;
        var h = frame.Height;
        var result = new Frame(w, h);

        for (var y = 0; y < h; y++)
            for (var x = 0; x < w; x++)
            {
                var sx = x + flow[y, x, 0] * t;
                var sy = y + flow[y, x, 1] * t;
                SampleBilinearReplicate(frame, sx, sy, result, x, y);
            }
        return result;
    }

    private static void SampleBilinearReplicate(Frame src, float sx, float sy, Frame dst, int dx, int dy)
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

        var o = (dy * w + dx) * 3;
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

    /// <summary>
    /// Bidirectional warp blend at fractional t (0=fa, 1=fb) with unsharp
    /// compensation for the bilinear blur that peaks at t=0.5. Direct port
    /// of _interpolate.
    /// </summary>
    public static Frame Interpolate(Frame fa, Frame fb, float[,,] flowAb, float[,,] flowBa, float t)
    {
        if (t <= 0f) return fa.Clone();
        if (t >= 1f) return fb.Clone();

        var warpedA = Apply(fa, flowAb, t);
        var warpedB = Apply(fb, flowBa, 1f - t);
        var blended = Blend(warpedA, 1f - t, warpedB, t);

        var sharpenStrength = 4f * t * (1f - t) * 0.45f;
        return sharpenStrength > 0.02f ? Unsharp(blended, sharpenStrength) : blended;
    }

    private static Frame Blend(Frame a, float alphaA, Frame b, float alphaB)
    {
        var result = new Frame(a.Width, a.Height);
        for (var i = 0; i < a.Bgr.Length; i++)
        {
            var v = a.Bgr[i] * alphaA + b.Bgr[i] * alphaB;
            result.Bgr[i] = (byte)Math.Clamp(v, 0, 255);
        }
        return result;
    }

    /// <summary>Unsharp mask: img + strength * (img - gaussianBlur(img, sigma=1.2)). Direct port of _unsharp.</summary>
    public static Frame Unsharp(Frame img, float strength)
    {
        var blurred = GaussianBlur(img, 1.2f);
        var result = new Frame(img.Width, img.Height);
        for (var i = 0; i < img.Bgr.Length; i++)
        {
            var v = (1f + strength) * img.Bgr[i] - strength * blurred.Bgr[i];
            result.Bgr[i] = (byte)Math.Clamp(v, 0, 255);
        }
        return result;
    }

    /// <summary>Separable Gaussian blur, kernel radius = ceil(3*sigma), matching cv2.GaussianBlur's default auto kernel size.</summary>
    private static Frame GaussianBlur(Frame img, float sigma)
    {
        var radius = Math.Max(1, (int)Math.Ceiling(3 * sigma));
        var kernel = new float[radius * 2 + 1];
        float sum = 0;
        for (var i = -radius; i <= radius; i++)
        {
            var v = MathF.Exp(-(i * i) / (2f * sigma * sigma));
            kernel[i + radius] = v;
            sum += v;
        }
        for (var i = 0; i < kernel.Length; i++) kernel[i] /= sum;

        var w = img.Width;
        var h = img.Height;
        var horiz = new Frame(w, h);
        for (var y = 0; y < h; y++)
            for (var x = 0; x < w; x++)
                for (var ch = 0; ch < 3; ch++)
                {
                    float acc = 0;
                    for (var k = -radius; k <= radius; k++)
                    {
                        var xc = Math.Clamp(x + k, 0, w - 1);
                        acc += img.Bgr[(y * w + xc) * 3 + ch] * kernel[k + radius];
                    }
                    horiz.Bgr[(y * w + x) * 3 + ch] = (byte)Math.Clamp(acc, 0, 255);
                }

        var result = new Frame(w, h);
        for (var y = 0; y < h; y++)
            for (var x = 0; x < w; x++)
                for (var ch = 0; ch < 3; ch++)
                {
                    float acc = 0;
                    for (var k = -radius; k <= radius; k++)
                    {
                        var yc = Math.Clamp(y + k, 0, h - 1);
                        acc += horiz.Bgr[(yc * w + x) * 3 + ch] * kernel[k + radius];
                    }
                    result.Bgr[(y * w + x) * 3 + ch] = (byte)Math.Clamp(acc, 0, 255);
                }
        return result;
    }
}

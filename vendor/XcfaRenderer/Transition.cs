using OpenCvSharp;

namespace XcfaRenderer;

/// <summary>
/// Segment-transition morphing: Farneback optical flow between the last
/// frame of one segment and the first frame of the next, computed at half
/// resolution then upscaled -- direct port of _render_worker.py's
/// _compute_morph_frames/_compute_flow (the CPU/_compute_flows_parallel
/// path specifically -- see the GPU note below). Called from two places in
/// this library: Renderer.cs's RenderSegmentMode (segment-to-segment cuts)
/// and SilenceLibrary.BuildPhase (morphing into each living-pause step) --
/// both match where the Python original calls _compute_morph_frames.
///
/// NOT ported: _compute_flows_gpu / _has_cuda (the Python original's
/// opportunistic CUDA path for this exact Farneback call). This is a hard
/// blocker, not a scope choice -- verified directly against the installed
/// package: OpenCvSharp4 4.13.0.20260627's managed OpenCvSharp.dll exposes
/// only CudaGpuMat/CudaHostMem (`grep -a -o "Cuda[A-Za-z]\{3,40\}"` over the
/// DLL), with no CudaFarnebackOpticalFlow or Cuda.* optical-flow API at all
/// -- so the call the Python original makes doesn't have a C# equivalent to
/// even attempt calling, let alone catch a failure from. nuget.org also has
/// no OpenCvSharp4.runtime.win.cuda (or similar) package alongside
/// OpenCvSharp4.runtime.win (confirmed via the NuGet search API) -- CUDA
/// support would require building/obtaining custom OpenCvSharp native
/// binaries outside NuGet, not a code change here.
/// </summary>
public static class Transition
{
    /// <summary>
    /// Returns the morph frames between fa and fb (not including fa/fb
    /// themselves). If faceRegion is given, morph-length is estimated from
    /// that ROI's pixel difference (clipped to [1, 5]); otherwise pass
    /// nFramesOverride to fix the count.
    /// </summary>
    public static List<Frame> ComputeMorphFrames(
        Frame fa, Frame fb,
        (int X, int Y, int W, int H)? faceRegion = null,
        int? nFramesOverride = null)
    {
        int n;
        if (nFramesOverride is { } over)
        {
            n = Math.Max(1, over);
        }
        else
        {
            var diff = MeanAbsDiff(fa, fb, faceRegion) / 255.0;
            n = (int)Math.Clamp(diff * 25, 1, 5);
        }

        var (flowAb, flowBa) = ComputeFullResFlowPair(fa, fb);

        var frames = new List<Frame>(n);
        for (var i = 1; i <= n; i++)
        {
            var t = (float)i / n; // t=1.0 on last frame -> equals fb (smooth landing, no snap)
            frames.Add(Warp.Interpolate(fa, fb, flowAb, flowBa, t));
        }
        return frames;
    }

    private static double MeanAbsDiff(Frame fa, Frame fb, (int X, int Y, int W, int H)? region)
    {
        var (x0, y0, w, h) = region is { } r ? r : (0, 0, fa.Width, fa.Height);
        double sum = 0;
        long count = 0;
        for (var y = y0; y < y0 + h; y++)
            for (var x = x0; x < x0 + w; x++)
            {
                var o = (y * fa.Width + x) * 3;
                for (var c = 0; c < 3; c++)
                {
                    sum += Math.Abs(fa.Bgr[o + c] - fb.Bgr[o + c]);
                    count++;
                }
            }
        return count == 0 ? 0 : sum / count;
    }

    private static (float[,,] Ab, float[,,] Ba) ComputeFullResFlowPair(Frame fa, Frame fb)
    {
        var w = fa.Width;
        var h = fa.Height;
        var halfW = Math.Max(1, w / 2);
        var halfH = Math.Max(1, h / 2);

        using var matA = ToMat(fa);
        using var matB = ToMat(fb);
        using var halfA = new Mat();
        using var halfB = new Mat();
        Cv2.Resize(matA, halfA, new Size(halfW, halfH));
        Cv2.Resize(matB, halfB, new Size(halfW, halfH));

        using var grayA = new Mat();
        using var grayB = new Mat();
        Cv2.CvtColor(halfA, grayA, ColorConversionCodes.BGR2GRAY);
        Cv2.CvtColor(halfB, grayB, ColorConversionCodes.BGR2GRAY);

        using var flowAbHalf = ComputeFlow(grayA, grayB);
        using var flowBaHalf = ComputeFlow(grayB, grayA);

        using var flowAbFull = new Mat();
        using var flowBaFull = new Mat();
        Cv2.Resize(flowAbHalf, flowAbFull, new Size(w, h));
        Cv2.Resize(flowBaHalf, flowBaFull, new Size(w, h));

        return (ToFlowArray(flowAbFull, 2.0f), ToFlowArray(flowBaFull, 2.0f));
    }

    private static Mat ComputeFlow(Mat grayA, Mat grayB)
    {
        var flow = new Mat();
        Cv2.CalcOpticalFlowFarneback(
            grayA, grayB, flow,
            pyrScale: 0.5, levels: 3, winsize: 15, iterations: 3,
            polyN: 5, polySigma: 1.2, flags: OpticalFlowFlags.None);
        return flow;
    }

    private static Mat ToMat(Frame frame)
    {
        var mat = new Mat(frame.Height, frame.Width, MatType.CV_8UC3);
        System.Runtime.InteropServices.Marshal.Copy(frame.Bgr, 0, mat.Data, frame.Bgr.Length);
        return mat;
    }

    private static float[,,] ToFlowArray(Mat flow, float scale)
    {
        var h = flow.Rows;
        var w = flow.Cols;
        var result = new float[h, w, 2];
        for (var y = 0; y < h; y++)
            for (var x = 0; x < w; x++)
            {
                var v = flow.At<Vec2f>(y, x);
                result[y, x, 0] = v.Item0 * scale;
                result[y, x, 1] = v.Item1 * scale;
            }
        return result;
    }
}

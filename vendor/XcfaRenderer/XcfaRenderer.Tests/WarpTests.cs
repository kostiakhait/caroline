using XcfaRenderer;

namespace XcfaRenderer.Tests;

public class WarpTests
{
    private static Frame SolidFrame(int w, int h, byte b, byte g, byte r)
    {
        var f = new Frame(w, h);
        for (var i = 0; i < w * h; i++)
        {
            f.Bgr[i * 3] = b;
            f.Bgr[i * 3 + 1] = g;
            f.Bgr[i * 3 + 2] = r;
        }
        return f;
    }

    [Fact]
    public void UpsampleFlowIdentityJustScalesByScale()
    {
        var raw = new sbyte[2, 2, 2];
        raw[0, 0, 0] = 4; raw[0, 0, 1] = -2;
        raw[1, 1, 0] = 6; raw[1, 1, 1] = 8;

        var result = Warp.UpsampleFlow(raw, scale: 0.5f, outW: 2, outH: 2);

        Assert.Equal(2.0f, result[0, 0, 0]);
        Assert.Equal(-1.0f, result[0, 0, 1]);
        Assert.Equal(3.0f, result[1, 1, 0]);
        Assert.Equal(4.0f, result[1, 1, 1]);
    }

    [Fact]
    public void ApplyWithZeroFlowReturnsSameContent()
    {
        var frame = SolidFrame(4, 4, 10, 20, 30);
        var flow = new float[4, 4, 2];

        var warped = Warp.Apply(frame, flow);

        Assert.Equal(frame.Bgr, warped.Bgr);
    }

    [Fact]
    public void InterpolateAtEndpointsReturnsInputFrames()
    {
        var fa = SolidFrame(2, 2, 0, 0, 0);
        var fb = SolidFrame(2, 2, 255, 255, 255);
        var flow = new float[2, 2, 2];

        var atZero = Warp.Interpolate(fa, fb, flow, flow, 0f);
        var atOne = Warp.Interpolate(fa, fb, flow, flow, 1f);

        Assert.Equal(fa.Bgr, atZero.Bgr);
        Assert.Equal(fb.Bgr, atOne.Bgr);
    }

    [Fact]
    public void InterpolateAtMidpointBlendsBetweenSolidColors()
    {
        var fa = SolidFrame(4, 4, 0, 0, 0);
        var fb = SolidFrame(4, 4, 200, 200, 200);
        var flow = new float[4, 4, 2]; // zero flow -- pure blend, no warp displacement

        var mid = Warp.Interpolate(fa, fb, flow, flow, 0.5f);

        // Unsharp on a flat solid-color image is a no-op (blurred == original),
        // so the midpoint should land close to the arithmetic average (~100).
        Assert.InRange(mid.Bgr[0], 90, 110);
    }

    [Fact]
    public void TranslateShiftsContentByGivenOffset()
    {
        var frame = new Frame(4, 4);
        // Put a distinct value at (2,2) so we can see it move.
        frame.Bgr[(2 * 4 + 2) * 3] = 255;

        var shifted = Warp.Translate(frame, dx: 1, dy: 0);

        // dst(x,y) = src(x-dx, y-dy) => the bright pixel at src(2,2) now appears at dst(3,2).
        Assert.Equal(255, shifted.Bgr[(2 * 4 + 3) * 3]);
    }
}

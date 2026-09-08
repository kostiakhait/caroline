using XcfaRenderer;

namespace XcfaRenderer.Tests;

public class TransitionTests
{
    private static Frame Checkerboard(int w, int h, int cell)
    {
        var f = new Frame(w, h);
        for (var y = 0; y < h; y++)
            for (var x = 0; x < w; x++)
            {
                var on = ((x / cell) + (y / cell)) % 2 == 0;
                var v = (byte)(on ? 220 : 30);
                var o = (y * w + x) * 3;
                f.Bgr[o] = v; f.Bgr[o + 1] = v; f.Bgr[o + 2] = v;
            }
        return f;
    }

    [Fact]
    public void IdenticalFramesProduceOneNearIdenticalMorphFrame()
    {
        var fa = Checkerboard(32, 32, 4);
        var fb = fa.Clone();

        var frames = Transition.ComputeMorphFrames(fa, fb);

        // mean-abs-diff between identical frames is 0 -> clip(0*25,1,5) = 1 frame.
        Assert.Single(frames);
    }

    [Fact]
    public void NFramesOverrideIsRespected()
    {
        var fa = Checkerboard(32, 32, 4);
        var fb = Checkerboard(32, 32, 8);

        var frames = Transition.ComputeMorphFrames(fa, fb, nFramesOverride: 3);

        Assert.Equal(3, frames.Count);
        Assert.All(frames, f => Assert.Equal((32, 32), (f.Width, f.Height)));
    }

    [Fact]
    public void MorphFramesHaveSameDimensionsAsInputs()
    {
        var fa = Checkerboard(20, 16, 4);
        var fb = Checkerboard(20, 16, 4);
        for (var i = 0; i < fb.Bgr.Length; i++) fb.Bgr[i] = (byte)(255 - fa.Bgr[i]);

        var frames = Transition.ComputeMorphFrames(fa, fb);

        Assert.NotEmpty(frames);
        Assert.All(frames, f => Assert.Equal((fa.Width, fa.Height), (f.Width, f.Height)));
    }
}

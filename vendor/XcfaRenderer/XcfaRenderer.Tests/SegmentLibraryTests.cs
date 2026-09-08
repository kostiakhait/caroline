using XcfaRenderer;

namespace XcfaRenderer.Tests;

public class SegmentLibraryTests
{
    private static XcfaCatalog BuildCatalog(params (int Id, float[][] AudioFeatures)[] segs)
    {
        var segments = segs.Select(s => new XcfaSegment
        {
            Id = s.Id,
            FrameCount = s.AudioFeatures.Length,
            MeanFeatures = Array.Empty<float>(),
            AudioFeatures = s.AudioFeatures,
            Blink = false,
            Height = 8,
            Width = 8,
            IsSilence = false,
            SilenceMul = 1f,
            Frames = new BlobRef[s.AudioFeatures.Length],
            Flows = Array.Empty<FlowRef>(),
        }).ToArray();

        return new XcfaCatalog
        {
            CfaVersion = 4,
            Version = 2,
            Fps = 5,
            FlowDiv = 8,
            Base = new BlobRef(0, 0),
            Segments = segments,
            HasBg = false,
        };
    }

    private static float[] Feat(params float[] mfccAndRest)
    {
        var v = new float[16];
        Array.Copy(mfccAndRest, v, Math.Min(mfccAndRest.Length, 16));
        return v;
    }

    [Fact]
    public void StaysSequentialWhenNextFrameIsAlmostAsGood()
    {
        // Segment 0 has 3 frames whose features drift smoothly; querying with
        // frame 1's own feature should stay on (0, cur+1) rather than jumping
        // to the globally-best (identical) match elsewhere, because sequential
        // continuation is only skipped when the alternative wins by more than
        // switchThreshold.
        var seg0 = new[] { Feat(1, 0, 0), Feat(0.9f, 0.1f, 0), Feat(0.8f, 0.2f, 0) };
        var catalog = BuildCatalog((0, seg0));
        var library = new SegmentLibrary(catalog);

        var pos = library.FindBestFrame(Feat(0.9f, 0.1f, 0), curSegId: 0, curFrameIdx: 0, switchThreshold: 0.12f);

        Assert.Equal(new FramePosition(0, 1), pos);
    }

    [Fact]
    public void JumpsWhenAlternativeSegmentIsClearlyBetter()
    {
        var seg0 = new[] { Feat(1, 0, 0), Feat(1, 0, 0) };
        var seg1 = new[] { Feat(0, 1, 0) };
        var catalog = BuildCatalog((0, seg0), (1, seg1));
        var library = new SegmentLibrary(catalog);

        // Querying with a feature that matches segment 1 far better than
        // staying sequential in segment 0 should jump.
        var pos = library.FindBestFrame(Feat(0, 1, 0), curSegId: 0, curFrameIdx: 0, switchThreshold: 0.12f);

        Assert.Equal(new FramePosition(1, 0), pos);
    }

    [Fact]
    public void SkipsAllZeroFeatureRows()
    {
        var seg0 = new[] { Feat(1, 0, 0), new float[16] /* all-zero = "no feature" */ };
        var catalog = BuildCatalog((0, seg0));
        var library = new SegmentLibrary(catalog);

        Assert.Equal(1, library.ValidFrameCount);
        Assert.False(library.HasFrame(new FramePosition(0, 1)));
    }

    [Fact]
    public void ReturnsCurrentPositionWhenLibraryIsEmpty()
    {
        var catalog = BuildCatalog((0, new[] { new float[16] }));
        var library = new SegmentLibrary(catalog);

        var pos = library.FindBestFrame(Feat(1, 0, 0), curSegId: 2, curFrameIdx: 5);

        Assert.Equal(new FramePosition(2, 5), pos);
    }
}

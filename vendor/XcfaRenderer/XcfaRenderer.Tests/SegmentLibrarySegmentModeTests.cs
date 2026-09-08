using XcfaRenderer;

namespace XcfaRenderer.Tests;

public class SegmentLibrarySegmentModeTests
{
    private static XcfaSegment MakeSegment(int id, float[] meanFeatures, bool blink = false)
    {
        return new XcfaSegment
        {
            Id = id,
            FrameCount = 1,
            MeanFeatures = meanFeatures,
            AudioFeatures = new[] { new float[16] },
            Blink = blink,
            Height = 8,
            Width = 8,
            IsSilence = false,
            SilenceMul = 0f,
            Frames = new[] { new BlobRef(0, 0) },
            Flows = Array.Empty<FlowRef>(),
        };
    }

    private static XcfaCatalog Catalog(params XcfaSegment[] segments) => new()
    {
        CfaVersion = 4, Version = 2, Fps = 5, FlowDiv = 8,
        Base = new BlobRef(0, 0), Segments = segments, HasBg = false,
    };

    private static float[] Vec(params float[] v)
    {
        var r = new float[16];
        Array.Copy(v, r, Math.Min(v.Length, 16));
        return r;
    }

    [Fact]
    public void CosineTopKRanksByMeanFeatureSimilarity()
    {
        var catalog = Catalog(
            MakeSegment(0, Vec(1, 0, 0)),
            MakeSegment(1, Vec(0, 1, 0)),
            MakeSegment(2, Vec(0.9f, 0.1f, 0)));
        var library = new SegmentLibrary(catalog);

        var top = library.CosineTopK(Vec(1, 0, 0), k: 3);

        // Position 0 is an exact match, position 2 is close, position 1 is orthogonal.
        Assert.Equal(new[] { 0, 2, 1 }, top);
    }

    [Fact]
    public void CosineTopKExcludesGivenPosition()
    {
        var catalog = Catalog(MakeSegment(0, Vec(1, 0, 0)), MakeSegment(1, Vec(0.9f, 0.1f, 0)));
        var library = new SegmentLibrary(catalog);

        var top = library.CosineTopK(Vec(1, 0, 0), k: 3, exclude: 0);

        Assert.Equal(new[] { 1 }, top);
    }

    [Fact]
    public void BestStitchAlwaysReturnsFirstCandidateForXcfaModels()
    {
        // XcfaLoader.py hardcodes thumbnails to "" for every .xcfa segment (see
        // SegmentLibrary.BestStitch's doc) -- so thumbnails are never available here,
        // and best_stitch must always take its "last_thumb is None" early return.
        var catalog = Catalog(MakeSegment(0, Vec(1, 0, 0)), MakeSegment(1, Vec(0, 1, 0)));
        var library = new SegmentLibrary(catalog);

        Assert.Equal(5, library.BestStitch(new List<int> { 5, 2, 9 }, lastSegPos: 0));
        Assert.Equal(2, library.BestStitch(new List<int> { 2 }, lastSegPos: -1));
    }

    [Fact]
    public void RandomBlinkSegmentPrefersBlinkTaggedPositions()
    {
        var catalog = Catalog(
            MakeSegment(0, Vec(1, 0, 0), blink: false),
            MakeSegment(1, Vec(0, 1, 0), blink: true),
            MakeSegment(2, Vec(0, 0, 1), blink: false));
        var library = new SegmentLibrary(catalog);
        var rng = new Random(42);

        for (var i = 0; i < 20; i++)
            Assert.Equal(1, library.RandomBlinkSegment(rng));
    }

    [Fact]
    public void IdAtMapsPositionToRealCatalogId()
    {
        // Deliberately non-sequential ids to exercise the position-vs-id distinction
        // documented on SegmentLibrary itself.
        var catalog = Catalog(MakeSegment(7, Vec(1, 0, 0)), MakeSegment(3, Vec(0, 1, 0)));
        var library = new SegmentLibrary(catalog);

        Assert.Equal(7, library.IdAt(0));
        Assert.Equal(3, library.IdAt(1));
    }
}

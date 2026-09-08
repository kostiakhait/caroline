using System.Globalization;
using MessagePack;
using XcfaRenderer;

namespace XcfaRenderer.Tests;

/// <summary>
/// Builds a tiny synthetic .xcfa (v2) file byte-for-byte matching the real
/// format (see XcfaFile.cs's header doc) so XcfaFile can be exercised
/// without needing one of the real, multi-hundred-MB models from
/// Caroline/art/models. One segment, two frames (one flow pair), no baked
/// alpha masks.
/// </summary>
public sealed class XcfaFileTests : IDisposable
{
    private readonly string _path;

    private static readonly byte[] BaseImageBytes = { 0xFF, 0xD8, 1, 2, 3 };
    private static readonly byte[] Frame0Bytes = { 0xFF, 0xD8, 10, 11 };
    private static readonly byte[] Frame1Bytes = { 0xFF, 0xD8, 20, 21, 22 };
    // fh8=fw8=1 (h=w=8, flow_div=8): forward (dy,dx) then backward (dy,dx), one int8 each.
    private static readonly sbyte[] FlowForward = { 5, -3 };
    private static readonly sbyte[] FlowBackward = { -7, 9 };
    private const float FlowScale = 0.5f;
    private static readonly float[] MeanFeatures = { 1.5f, -2.5f, 3.5f };
    private static readonly float[] AudioFeatureRow = Enumerable.Range(0, 16).Select(i => (float)i).ToArray();

    public XcfaFileTests()
    {
        _path = Path.Combine(Path.GetTempPath(), $"xcfa_test_{Guid.NewGuid():N}.xcfa");
        File.WriteAllBytes(_path, BuildSyntheticXcfa());
    }

    private static byte[] BuildSyntheticXcfa()
    {
        using var blobs = new MemoryStream();
        long Append(byte[] data)
        {
            var offset = blobs.Position;
            blobs.Write(data);
            return offset;
        }

        var baseOff = Append(BaseImageBytes);
        var frame0Off = Append(Frame0Bytes);
        var frame1Off = Append(Frame1Bytes);
        var flowBytes = FlowForward.Select(b => unchecked((byte)b))
            .Concat(FlowBackward.Select(b => unchecked((byte)b))).ToArray();
        var flowOff = Append(flowBytes);

        var mfBase64 = Convert.ToBase64String(FloatsToBytes(MeanFeatures));
        var afBase64 = Convert.ToBase64String(FloatsToBytes(AudioFeatureRow));

        string F(float v) => v.ToString(CultureInfo.InvariantCulture);
        var json = $$"""
        {
          "cfa_v": 4, "v": 2, "fps": 5.0, "flow_div": 8,
          "base": [{{baseOff}}, {{BaseImageBytes.Length}}],
          "segments": [
            {
              "id": 0, "n": 2,
              "mf": "{{mfBase64}}",
              "af": "{{afBase64}}",
              "blink": false, "h": 8, "w": 8, "sil": false, "sil_mul": 1.0,
              "frames": [[{{frame0Off}}, {{Frame0Bytes.Length}}], [{{frame1Off}}, {{Frame1Bytes.Length}}]],
              "flows": [[{{flowOff}}, {{flowBytes.Length}}, {{F(FlowScale)}}]]
            }
          ],
          "has_bg": false
        }
        """;
        var catalogBytes = MessagePackSerializer.ConvertFromJson(json);

        using var file = new MemoryStream();
        file.Write(System.Text.Encoding.ASCII.GetBytes("XCFA"));
        file.Write(BitConverter.GetBytes((uint)2));
        var catalogOffsetPos = file.Position;
        file.Write(BitConverter.GetBytes((ulong)0)); // patched below
        var blobsArray = blobs.ToArray();
        file.Write(blobsArray);
        var catalogOffset = file.Position;
        file.Write(catalogBytes);
        file.Write(BitConverter.GetBytes((ulong)catalogBytes.Length));

        var result = file.ToArray();
        BitConverter.GetBytes((ulong)catalogOffset).CopyTo(result, (int)catalogOffsetPos);
        return result;
    }

    private static byte[] FloatsToBytes(float[] floats)
    {
        var bytes = new byte[floats.Length * sizeof(float)];
        Buffer.BlockCopy(floats, 0, bytes, 0, bytes.Length);
        return bytes;
    }

    [Fact]
    public void ParsesHeaderAndCatalogFields()
    {
        using var xcfa = XcfaFile.Open(_path);
        Assert.Equal(2, xcfa.Catalog.Version);
        Assert.Equal(4, xcfa.Catalog.CfaVersion);
        Assert.Equal(5f, xcfa.Catalog.Fps);
        Assert.Equal(8, xcfa.Catalog.FlowDiv);
        Assert.False(xcfa.Catalog.HasBg);
        Assert.Null(xcfa.Catalog.BaseBg);
        Assert.Single(xcfa.Catalog.Segments);
    }

    [Fact]
    public void ParsesSegmentFields()
    {
        using var xcfa = XcfaFile.Open(_path);
        var seg = xcfa.Catalog.Segments[0];
        Assert.Equal(0, seg.Id);
        Assert.Equal(2, seg.FrameCount);
        Assert.Equal(8, seg.Height);
        Assert.Equal(8, seg.Width);
        Assert.False(seg.Blink);
        Assert.False(seg.IsSilence);
        Assert.Equal(1f, seg.SilenceMul);
        Assert.Equal(MeanFeatures, seg.MeanFeatures);
        Assert.Equal(2, seg.Frames.Length);
        Assert.Single(seg.Flows);
        Assert.Null(seg.BgFrames);
    }

    [Fact]
    public void DecodesAudioFeaturesRow()
    {
        using var xcfa = XcfaFile.Open(_path);
        var seg = xcfa.Catalog.Segments[0];
        Assert.Single(seg.AudioFeatures);
        Assert.Equal(AudioFeatureRow, seg.AudioFeatures[0]);
    }

    [Fact]
    public void ReadsBaseImageAndFrameBlobsExactly()
    {
        using var xcfa = XcfaFile.Open(_path);
        Assert.Equal(BaseImageBytes, xcfa.ReadBaseImage());
        var seg = xcfa.Catalog.Segments[0];
        Assert.Equal(Frame0Bytes, xcfa.ReadFrame(seg, 0));
        Assert.Equal(Frame1Bytes, xcfa.ReadFrame(seg, 1));
    }

    [Fact]
    public void DecodesFlowPairWithCorrectShapeAndValues()
    {
        using var xcfa = XcfaFile.Open(_path);
        var seg = xcfa.Catalog.Segments[0];
        var (forward, backward, scale) = xcfa.ReadFlowPair(seg, 0);

        Assert.Equal(FlowScale, scale);
        Assert.Equal(1, forward.GetLength(0));
        Assert.Equal(1, forward.GetLength(1));
        Assert.Equal(2, forward.GetLength(2));
        Assert.Equal(FlowForward[0], forward[0, 0, 0]);
        Assert.Equal(FlowForward[1], forward[0, 0, 1]);
        Assert.Equal(FlowBackward[0], backward[0, 0, 0]);
        Assert.Equal(FlowBackward[1], backward[0, 0, 1]);
    }

    [Fact]
    public void ReadBaseBgMaskThrowsWhenNoBgBaked()
    {
        using var xcfa = XcfaFile.Open(_path);
        Assert.Throws<InvalidOperationException>(() => xcfa.ReadBaseBgMask());
    }

    public void Dispose()
    {
        try { File.Delete(_path); } catch { /* best effort */ }
    }
}

using System.Text.Json;

namespace XcfaRenderer;

/// <summary>A single (offset, size) blob location inside the .xcfa file.</summary>
public readonly record struct BlobRef(long Offset, long Size);

/// <summary>A single (offset, size, scale) flow-pair blob location.</summary>
public readonly record struct FlowRef(long Offset, long Size, float Scale);

public sealed class XcfaSegment
{
    public required int Id { get; init; }
    public required int FrameCount { get; init; }
    public required float[] MeanFeatures { get; init; }
    /// <summary>Per-frame 16-dim audio features (n_frames x 16); a row of all-zero means "no feature" (silent/padding).</summary>
    public required float[][] AudioFeatures { get; init; }
    public required bool Blink { get; init; }
    public required int Height { get; init; }
    public required int Width { get; init; }
    public required bool IsSilence { get; init; }
    public required float SilenceMul { get; init; }
    public required BlobRef[] Frames { get; init; }
    public required FlowRef[] Flows { get; init; }
    /// <summary>Pre-baked alpha-mask PNG blobs, one per frame -- present only if the catalog's HasBg is true.</summary>
    public BlobRef[]? BgFrames { get; init; }
}

/// <summary>Face region as fractions (0..1) of output width/height -- multiply by out_w/out_h to get pixel coords, matching _render_worker.py's fr_x/fr_y/fr_w/fr_h.</summary>
public readonly record struct FaceRegion(float XFrac, float YFrac, float WFrac, float HFrac);

public sealed class XcfaCatalog
{
    public required int CfaVersion { get; init; }
    public required int Version { get; init; }
    public required float Fps { get; init; }
    public required int FlowDiv { get; init; }
    public required BlobRef Base { get; init; }
    public required XcfaSegment[] Segments { get; init; }
    public required bool HasBg { get; init; }
    /// <summary>Pre-baked alpha-mask PNG blob for the base image -- present only if HasBg is true.</summary>
    public BlobRef? BaseBg { get; init; }
    /// <summary>Null means "no face region -- full-frame compositing" (matches the Python model's face_region being optional).</summary>
    public FaceRegion? FaceRegion { get; init; }
}

/// <summary>
/// Reads the .xcfa v2 binary container: a 16-byte header (magic, version,
/// catalog offset) followed by data blobs (base-image JPEG, per-segment
/// frame JPEGs, int8 flow blobs, optional PNG alpha masks), then a msgpack
/// catalog, then an 8-byte trailing catalog-length sentinel. Direct port of
/// XcfaLoader.py's _open_xcfa/loadModel -- playback-only, no writing.
/// </summary>
public sealed class XcfaFile : IDisposable
{
    private const string Magic = "XCFA";
    private readonly FileStream _stream;

    public XcfaCatalog Catalog { get; }

    private XcfaFile(FileStream stream, XcfaCatalog catalog)
    {
        _stream = stream;
        Catalog = catalog;
    }

    public static XcfaFile Open(string path)
    {
        var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        try
        {
            Span<byte> header = stackalloc byte[16];
            ReadExact(stream, header);
            var magic = System.Text.Encoding.ASCII.GetString(header[..4]);
            if (magic != Magic)
                throw new InvalidDataException($"Not an .xcfa file (magic was \"{magic}\", expected \"{Magic}\").");
            var version = BitConverter.ToUInt32(header[4..8]);
            if (version != 2)
                throw new NotSupportedException($".xcfa version {version} is not supported (only v2).");
            var catalogOffset = (long)BitConverter.ToUInt64(header[8..16]);

            stream.Seek(-8, SeekOrigin.End);
            Span<byte> tail = stackalloc byte[8];
            ReadExact(stream, tail);
            var catalogLen = (long)BitConverter.ToUInt64(tail);

            stream.Seek(catalogOffset, SeekOrigin.Begin);
            var catalogBytes = new byte[catalogLen];
            ReadExact(stream, catalogBytes);

            var catalog = ParseCatalog(catalogBytes);
            return new XcfaFile(stream, catalog);
        }
        catch
        {
            stream.Dispose();
            throw;
        }
    }

    private static void ReadExact(FileStream stream, Span<byte> buffer)
    {
        var total = 0;
        while (total < buffer.Length)
        {
            var n = stream.Read(buffer[total..]);
            if (n == 0) throw new EndOfStreamException("Unexpected end of .xcfa file.");
            total += n;
        }
    }

    private static XcfaCatalog ParseCatalog(byte[] catalogBytes)
    {
        // Converting msgpack -> JSON and parsing with System.Text.Json sidesteps
        // MessagePack-CSharp's typeless/contract resolvers entirely -- the
        // catalog is an arbitrary Python dict (msgpack map), not a fixed
        // schema we control on the writer side, so this is both simpler and
        // more tolerant of unknown/optional keys than a formatter-based
        // approach would be.
        var json = MessagePack.MessagePackSerializer.ConvertToJson(catalogBytes);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;

        var hasBg = root.TryGetProperty("has_bg", out var hasBgEl) && hasBgEl.GetBoolean();

        FaceRegion? faceRegion = null;
        if (root.TryGetProperty("face_region", out var frEl) && frEl.ValueKind == JsonValueKind.Object)
        {
            faceRegion = new FaceRegion(
                frEl.GetProperty("x_frac").GetSingle(),
                frEl.GetProperty("y_frac").GetSingle(),
                frEl.GetProperty("w_frac").GetSingle(),
                frEl.GetProperty("h_frac").GetSingle());
        }

        return new XcfaCatalog
        {
            CfaVersion = root.TryGetProperty("cfa_v", out var cfaV) ? cfaV.GetInt32() : 4,
            Version = root.GetProperty("v").GetInt32(),
            Fps = root.TryGetProperty("fps", out var fpsEl) ? fpsEl.GetSingle() : 5f,
            FlowDiv = root.TryGetProperty("flow_div", out var flowDivEl) ? flowDivEl.GetInt32() : 8,
            Base = ReadBlobRef(root.GetProperty("base")),
            Segments = root.GetProperty("segments").EnumerateArray().Select(ParseSegment).ToArray(),
            HasBg = hasBg,
            BaseBg = hasBg && root.TryGetProperty("base_bg", out var baseBgEl) ? ReadBlobRef(baseBgEl) : null,
            FaceRegion = faceRegion,
        };
    }

    private static XcfaSegment ParseSegment(JsonElement seg)
    {
        return new XcfaSegment
        {
            Id = seg.GetProperty("id").GetInt32(),
            FrameCount = seg.GetProperty("n").GetInt32(),
            MeanFeatures = ReadFloatBlob(seg.GetProperty("mf")),
            AudioFeatures = ReadAudioFeatures(seg.GetProperty("af")),
            Blink = seg.TryGetProperty("blink", out var blinkEl) && blinkEl.GetBoolean(),
            Height = seg.GetProperty("h").GetInt32(),
            Width = seg.GetProperty("w").GetInt32(),
            // Matches XcfaLoader.py:120-123 exactly: is_silence is true if either "sil" is truthy
            // OR "sil_mul" is present at all (even 0.0) -- the two flags aren't the same condition.
            IsSilence = (seg.TryGetProperty("sil", out var silEl) && silEl.GetBoolean())
                || seg.TryGetProperty("sil_mul", out _),
            SilenceMul = seg.TryGetProperty("sil_mul", out var silMulEl) ? silMulEl.GetSingle() : 0f,
            Frames = seg.GetProperty("frames").EnumerateArray().Select(ReadBlobRef).ToArray(),
            Flows = seg.GetProperty("flows").EnumerateArray().Select(ReadFlowRef).ToArray(),
            BgFrames = seg.TryGetProperty("bg_frames", out var bgFramesEl)
                ? bgFramesEl.EnumerateArray().Select(ReadBlobRef).ToArray()
                : null,
        };
    }

    private static BlobRef ReadBlobRef(JsonElement pair)
    {
        var arr = pair.EnumerateArray().ToArray();
        return new BlobRef(arr[0].GetInt64(), arr[1].GetInt64());
    }

    private static FlowRef ReadFlowRef(JsonElement triple)
    {
        var arr = triple.EnumerateArray().ToArray();
        return new FlowRef(arr[0].GetInt64(), arr[1].GetInt64(), arr[2].GetSingle());
    }

    // msgpack bin (raw bytes) round-trips through ConvertToJson as a base64 string.
    private static float[] ReadFloatBlob(JsonElement binAsBase64)
    {
        var bytes = Convert.FromBase64String(binAsBase64.GetString()!);
        var floats = new float[bytes.Length / sizeof(float)];
        Buffer.BlockCopy(bytes, 0, floats, 0, bytes.Length);
        return floats;
    }

    private static float[][] ReadAudioFeatures(JsonElement binAsBase64)
    {
        var flat = ReadFloatBlob(binAsBase64);
        const int dims = 16;
        var n = flat.Length / dims;
        var result = new float[n][];
        for (var i = 0; i < n; i++)
        {
            var row = new float[dims];
            Array.Copy(flat, i * dims, row, 0, dims);
            result[i] = row;
        }
        return result;
    }

    private byte[] ReadBlob(BlobRef blob)
    {
        _stream.Seek(blob.Offset, SeekOrigin.Begin);
        var buffer = new byte[blob.Size];
        ReadExact(_stream, buffer);
        return buffer;
    }

    /// <summary>The base-image JPEG bytes.</summary>
    public byte[] ReadBaseImage() => ReadBlob(Catalog.Base);

    /// <summary>The base image's pre-baked alpha-mask PNG bytes (only valid when Catalog.HasBg).</summary>
    public byte[] ReadBaseBgMask()
    {
        if (Catalog.BaseBg is not { } bg)
            throw new InvalidOperationException("This .xcfa has no baked alpha masks (HasBg is false).");
        return ReadBlob(bg);
    }

    /// <summary>A single segment frame's JPEG bytes.</summary>
    public byte[] ReadFrame(XcfaSegment segment, int frameIndex) => ReadBlob(segment.Frames[frameIndex]);

    /// <summary>A segment frame's pre-baked alpha-mask PNG bytes (only valid when Catalog.HasBg).</summary>
    public byte[] ReadFrameBgMask(XcfaSegment segment, int frameIndex)
    {
        if (segment.BgFrames is not { } bgFrames)
            throw new InvalidOperationException("This segment has no baked alpha masks (HasBg is false).");
        return ReadBlob(bgFrames[frameIndex]);
    }

    /// <summary>
    /// Decodes flow-pair index <paramref name="pairIndex"/> (between frame
    /// pairIndex and pairIndex+1) into (forward, backward) RAW int8
    /// displacement grids, each shaped (fh8, fw8, 2) with channel 0 = dx,
    /// channel 1 = dy (matches XcfaLoader.py's decodeFlowPair/_upsample_flow:
    /// "up[..., 0] *= out_w / fw8  # dx", "up[..., 1] *= out_h / fh8  # dy").
    /// Values are NOT yet multiplied by Scale and NOT yet upsampled -- callers
    /// do real_pixel = int8 * Scale, then upsample to full segment resolution
    /// (see Warp.UpsampleFlow), same split of responsibility as the Python
    /// original.
    /// </summary>
    public (sbyte[,,] Forward, sbyte[,,] Backward, float Scale) ReadFlowPair(XcfaSegment segment, int pairIndex)
    {
        var flowRef = segment.Flows[pairIndex];
        var raw = ReadBlob(new BlobRef(flowRef.Offset, flowRef.Size));

        var fh8 = Math.Max(1, segment.Height / Catalog.FlowDiv);
        var fw8 = Math.Max(1, segment.Width / Catalog.FlowDiv);
        var expected = 2 * fh8 * fw8 * 2;
        if (raw.Length != expected)
            throw new InvalidDataException($"Flow blob size {raw.Length} != expected {expected} (fh8={fh8}, fw8={fw8}).");

        var forward = new sbyte[fh8, fw8, 2];
        var backward = new sbyte[fh8, fw8, 2];
        var idx = 0;
        for (var y = 0; y < fh8; y++)
            for (var x = 0; x < fw8; x++)
                for (var c = 0; c < 2; c++)
                    forward[y, x, c] = unchecked((sbyte)raw[idx++]);
        for (var y = 0; y < fh8; y++)
            for (var x = 0; x < fw8; x++)
                for (var c = 0; c < 2; c++)
                    backward[y, x, c] = unchecked((sbyte)raw[idx++]);

        return (forward, backward, flowRef.Scale);
    }

    public void Dispose() => _stream.Dispose();
}

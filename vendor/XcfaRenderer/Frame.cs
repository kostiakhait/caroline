using SkiaSharp;

namespace XcfaRenderer;

/// <summary>
/// A decoded BGR24 frame buffer (3 bytes/pixel, row-major, no alpha) -- the
/// pixel format every stage of this library shares (JPEG decode, warp,
/// compositing) and the exact format ffmpeg's rawvideo "bgr24" input expects
/// (see Encoder.cs), so frames need no conversion on their way to the pipe.
/// </summary>
public sealed class Frame
{
    public int Width { get; }
    public int Height { get; }
    public byte[] Bgr { get; }

    public Frame(int width, int height)
    {
        Width = width;
        Height = height;
        Bgr = new byte[width * height * 3];
    }

    private Frame(int width, int height, byte[] bgr)
    {
        Width = width;
        Height = height;
        Bgr = bgr;
    }

    public Frame Clone() => new(Width, Height, (byte[])Bgr.Clone());

    /// <summary>Decodes JPEG bytes to BGR24, resizing to (width, height) if the decoded size differs.</summary>
    public static Frame DecodeJpeg(byte[] jpegBytes, int width, int height)
    {
        using var bitmap = SKBitmap.Decode(jpegBytes)
            ?? throw new InvalidDataException("Failed to decode JPEG frame.");
        return FromBitmap(bitmap, width, height);
    }

    /// <summary>Decodes JPEG bytes at their own native resolution -- used when the target output size isn't known yet (e.g. the base image, which determines it).</summary>
    public static Frame DecodeJpegNative(byte[] jpegBytes)
    {
        using var bitmap = SKBitmap.Decode(jpegBytes)
            ?? throw new InvalidDataException("Failed to decode JPEG frame.");
        return FromBitmap(bitmap, bitmap.Width, bitmap.Height);
    }

    /// <summary>Decodes a PNG mask (grayscale or alpha-carrying, e.g. a baked bg mask) to a raw byte[] at (width, height); 0 = transparent, 255 = opaque.</summary>
    public static byte[] DecodeMaskPng(byte[] pngBytes, int width, int height)
    {
        using var bitmap = SKBitmap.Decode(pngBytes)
            ?? throw new InvalidDataException("Failed to decode PNG mask.");
        using var canvasBmp = ResampleTo(bitmap, width, height, SKColorType.Bgra8888);
        var span = canvasBmp.GetPixelSpan();

        var mask = new byte[width * height];
        var hasAlpha = bitmap.AlphaType != SKAlphaType.Opaque;
        for (var i = 0; i < width * height; i++)
        {
            var o = i * 4; // B,G,R,A
            mask[i] = hasAlpha ? span[o + 3] : span[o + 2]; // alpha if present, else red (grayscale channel)
        }
        return mask;
    }

    private static Frame FromBitmap(SKBitmap bitmap, int width, int height)
    {
        using var canvasBmp = ResampleTo(bitmap, width, height, SKColorType.Bgra8888);
        var span = canvasBmp.GetPixelSpan();

        var frame = new Frame(width, height);
        for (var i = 0; i < width * height; i++)
        {
            var src = i * 4;
            var dst = i * 3;
            frame.Bgr[dst] = span[src];         // B
            frame.Bgr[dst + 1] = span[src + 1]; // G
            frame.Bgr[dst + 2] = span[src + 2]; // R
        }
        return frame;
    }

    /// <summary>Draws bitmap onto a (width, height) canvas of the given color type -- handles both resize and format conversion in one GPU-free raster pass.</summary>
    private static SKBitmap ResampleTo(SKBitmap bitmap, int width, int height, SKColorType colorType)
    {
        var info = new SKImageInfo(width, height, colorType, SKAlphaType.Unpremul);
        var target = new SKBitmap(info);
        using var canvas = new SKCanvas(target);
        canvas.DrawBitmap(bitmap, new SKRect(0, 0, width, height), new SKSamplingOptions(SKFilterMode.Linear));
        return target;
    }
}

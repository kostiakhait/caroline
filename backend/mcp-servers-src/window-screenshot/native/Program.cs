using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Text.Json;

internal static class Program
{
    [DllImport("user32.dll")]
    private static extern bool SetProcessDPIAware();

    [DllImport("user32.dll")]
    private static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

    [DllImport("user32.dll")]
    private static extern bool PrintWindow(IntPtr hWnd, IntPtr hdc, uint nFlags);

    // Renders the window's real current content into the given DC regardless of Z-order/
    // occlusion/foreground state — this is what lets a background, non-active window be
    // captured at all (a screen-crop approach would just capture whatever's on top of it).
    private const uint PW_RENDERFULLCONTENT = 0x00000002;

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    private class FrameInfo
    {
        public int Index { get; set; }
        public string File { get; set; } = "";
        public long ElapsedMs { get; set; }
    }

    private static IntPtr ParseHwnd(string s)
    {
        var trimmed = s.StartsWith("0x", StringComparison.OrdinalIgnoreCase) ? s[2..] : s;
        return new IntPtr(Convert.ToInt64(trimmed, 16));
    }

    private static Bitmap CaptureWindowBitmap(IntPtr hwnd, out int width, out int height)
    {
        if (!GetWindowRect(hwnd, out var rect))
        {
            throw new InvalidOperationException($"GetWindowRect failed for handle {hwnd} - is it a valid window?");
        }
        width = rect.Right - rect.Left;
        height = rect.Bottom - rect.Top;
        if (width <= 0 || height <= 0)
        {
            throw new InvalidOperationException($"Window has non-positive size ({width}x{height}) - it may be minimized.");
        }

        var bitmap = new Bitmap(width, height);
        using (var graphics = Graphics.FromImage(bitmap))
        {
            var hdc = graphics.GetHdc();
            try
            {
                PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT);
            }
            finally
            {
                graphics.ReleaseHdc(hdc);
            }
        }
        return bitmap;
    }

    // Crops to (cropX,cropY,cropWidth,cropHeight) if all four are given (clamped to the source
    // bitmap's own bounds), then downscales proportionally if maxWidth is given and exceeded.
    // Disposes the input bitmap if it returns a different instance. Same logic as screenshot's
    // native/Program.cs - kept duplicated rather than shared, matching this repo's convention.
    private static Bitmap ApplyCropAndDownscale(Bitmap source, int? cropX, int? cropY, int? cropWidth, int? cropHeight, int? maxWidth)
    {
        var current = source;

        if (cropX is not null && cropY is not null && cropWidth is not null && cropHeight is not null)
        {
            var cropRect = Rectangle.Intersect(
                new Rectangle(cropX.Value, cropY.Value, cropWidth.Value, cropHeight.Value),
                new Rectangle(0, 0, current.Width, current.Height));
            if (cropRect.Width <= 0 || cropRect.Height <= 0)
            {
                throw new InvalidOperationException("Crop rectangle does not intersect the captured bitmap.");
            }
            var cropped = current.Clone(cropRect, current.PixelFormat);
            if (!ReferenceEquals(current, source)) current.Dispose();
            current = cropped;
        }

        if (maxWidth is not null && current.Width > maxWidth.Value)
        {
            var scale = (double)maxWidth.Value / current.Width;
            var newWidth = maxWidth.Value;
            var newHeight = Math.Max(1, (int)Math.Round(current.Height * scale));
            var resized = new Bitmap(newWidth, newHeight);
            using (var g = Graphics.FromImage(resized))
            {
                g.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.HighQualityBicubic;
                g.DrawImage(current, 0, 0, newWidth, newHeight);
            }
            if (!ReferenceEquals(current, source)) current.Dispose();
            current = resized;
        }

        return current;
    }

    private static int Main(string[] args)
    {
        // Same rationale as the other coordinate/rect-handling servers in this repo: without
        // this, GetWindowRect operates in a DPI-virtualized space that can mismatch real pixels.
        SetProcessDPIAware();

        string? action = null;
        string? hwndArg = null;
        string? outFile = null;
        string? outDir = null;
        int count = 1;
        int intervalMs = 1000;
        int? cropX = null, cropY = null, cropWidth = null, cropHeight = null;
        int? maxWidth = null;

        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--action": action = args[++i]; break;
                case "--hwnd": hwndArg = args[++i]; break;
                case "--out": outFile = args[++i]; break;
                case "--outDir": outDir = args[++i]; break;
                case "--count": count = int.Parse(args[++i]); break;
                case "--intervalMs": intervalMs = int.Parse(args[++i]); break;
                case "--cropX": cropX = int.Parse(args[++i]); break;
                case "--cropY": cropY = int.Parse(args[++i]); break;
                case "--cropWidth": cropWidth = int.Parse(args[++i]); break;
                case "--cropHeight": cropHeight = int.Parse(args[++i]); break;
                case "--maxWidth": maxWidth = int.Parse(args[++i]); break;
            }
        }

        if (action is null)
        {
            Console.Error.WriteLine("Missing required --action");
            return 1;
        }
        if (hwndArg is null)
        {
            Console.Error.WriteLine("Missing required --hwnd");
            return 1;
        }
        var hwnd = ParseHwnd(hwndArg);

        try
        {
            switch (action)
            {
                case "capture":
                {
                    if (outFile is null)
                    {
                        Console.Error.WriteLine("Missing required --out <path>");
                        return 1;
                    }
                    using var bitmap = CaptureWindowBitmap(hwnd, out _, out _);
                    using var final = ApplyCropAndDownscale(bitmap, cropX, cropY, cropWidth, cropHeight, maxWidth);
                    final.Save(outFile, ImageFormat.Png);
                    Console.WriteLine($"{final.Width}x{final.Height}");
                    return 0;
                }
                case "burst":
                {
                    if (outDir is null)
                    {
                        Console.Error.WriteLine("Missing required --outDir <path>");
                        return 1;
                    }
                    Directory.CreateDirectory(outDir);

                    var digits = Math.Max(4, count.ToString().Length);
                    var frames = new List<FrameInfo>();
                    var sw = Stopwatch.StartNew();

                    for (var i = 0; i < count; i++)
                    {
                        var frameStart = sw.ElapsedMilliseconds;
                        using var bitmap = CaptureWindowBitmap(hwnd, out _, out _);
                        var fileName = $"frame_{(i + 1).ToString().PadLeft(digits, '0')}.png";
                        var filePath = Path.Combine(outDir, fileName);
                        bitmap.Save(filePath, ImageFormat.Png);
                        frames.Add(new FrameInfo { Index = i + 1, File = filePath, ElapsedMs = frameStart });

                        if (i < count - 1)
                        {
                            var captureTime = sw.ElapsedMilliseconds - frameStart;
                            var sleepMs = intervalMs - captureTime;
                            if (sleepMs > 0) Thread.Sleep((int)sleepMs);
                        }
                    }

                    var summary = new { count, intervalMs, savePath = outDir, frames };
                    Console.WriteLine(JsonSerializer.Serialize(summary, new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase }));
                    return 0;
                }
                default:
                    Console.Error.WriteLine($"Unknown action: {action}");
                    return 1;
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"windowscreenshot.exe failed: {ex.Message}");
            return 1;
        }
    }
}

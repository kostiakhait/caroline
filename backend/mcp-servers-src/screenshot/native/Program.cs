using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Windows.Forms;

internal static class Program
{
    [DllImport("user32.dll")]
    private static extern bool SetProcessDPIAware();

    private static int Main(string[] args)
    {
        // Without this, the process is DPI-virtualized by Windows and CopyFromScreen
        // can return a scaled/substituted bitmap of the foreground window instead of
        // the real full-desktop pixels.
        SetProcessDPIAware();

        int? monitor = null;
        string? outFile = null;
        int? cropX = null, cropY = null, cropWidth = null, cropHeight = null;
        int? maxWidth = null;

        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--monitor":
                    monitor = int.Parse(args[++i]);
                    break;
                case "--out":
                    outFile = args[++i];
                    break;
                case "--cropX": cropX = int.Parse(args[++i]); break;
                case "--cropY": cropY = int.Parse(args[++i]); break;
                case "--cropWidth": cropWidth = int.Parse(args[++i]); break;
                case "--cropHeight": cropHeight = int.Parse(args[++i]); break;
                case "--maxWidth": maxWidth = int.Parse(args[++i]); break;
            }
        }

        if (outFile is null)
        {
            Console.Error.WriteLine("Missing required --out <path>");
            return 1;
        }

        Rectangle bounds;
        if (monitor is { } index)
        {
            var screens = Screen.AllScreens;
            if (index < 0 || index >= screens.Length)
            {
                Console.Error.WriteLine($"Monitor index {index} out of range (0..{screens.Length - 1})");
                return 1;
            }
            bounds = screens[index].Bounds;
        }
        else
        {
            bounds = SystemInformation.VirtualScreen;
        }

        using var bitmap = new Bitmap(bounds.Width, bounds.Height);
        using (var graphics = Graphics.FromImage(bitmap))
        {
            graphics.CopyFromScreen(bounds.Location, Point.Empty, bounds.Size);
        }

        Bitmap final = bitmap;
        try
        {
            if (cropX is not null && cropY is not null && cropWidth is not null && cropHeight is not null)
            {
                var cropRect = Rectangle.Intersect(
                    new Rectangle(cropX.Value, cropY.Value, cropWidth.Value, cropHeight.Value),
                    new Rectangle(0, 0, final.Width, final.Height));
                if (cropRect.Width <= 0 || cropRect.Height <= 0)
                {
                    Console.Error.WriteLine("Crop rectangle does not intersect the captured bitmap.");
                    return 1;
                }
                var cropped = final.Clone(cropRect, final.PixelFormat);
                if (!ReferenceEquals(final, bitmap)) final.Dispose();
                final = cropped;
            }

            if (maxWidth is not null && final.Width > maxWidth.Value)
            {
                var scale = (double)maxWidth.Value / final.Width;
                var newWidth = maxWidth.Value;
                var newHeight = Math.Max(1, (int)Math.Round(final.Height * scale));
                var resized = new Bitmap(newWidth, newHeight);
                using (var g = Graphics.FromImage(resized))
                {
                    g.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.HighQualityBicubic;
                    g.DrawImage(final, 0, 0, newWidth, newHeight);
                }
                if (!ReferenceEquals(final, bitmap)) final.Dispose();
                final = resized;
            }

            final.Save(outFile, ImageFormat.Png);
            Console.WriteLine($"{final.Width}x{final.Height}");
            return 0;
        }
        finally
        {
            if (!ReferenceEquals(final, bitmap)) final.Dispose();
        }
    }
}

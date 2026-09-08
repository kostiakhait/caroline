using System.Runtime.InteropServices;

internal static class Program
{
    [DllImport("user32.dll")]
    private static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);

    private const uint WM_LBUTTONDOWN = 0x0201;
    private const uint WM_LBUTTONUP = 0x0202;
    private const uint WM_RBUTTONDOWN = 0x0204;
    private const uint WM_RBUTTONUP = 0x0205;
    private const uint WM_MBUTTONDOWN = 0x0207;
    private const uint WM_MBUTTONUP = 0x0208;

    private const int MK_LBUTTON = 0x0001;
    private const int MK_RBUTTON = 0x0002;
    private const int MK_MBUTTON = 0x0010;

    private static (uint down, uint up, IntPtr wParamDown) ButtonMessages(string button) => button switch
    {
        "Left" => (WM_LBUTTONDOWN, WM_LBUTTONUP, (IntPtr)MK_LBUTTON),
        "Right" => (WM_RBUTTONDOWN, WM_RBUTTONUP, (IntPtr)MK_RBUTTON),
        "Middle" => (WM_MBUTTONDOWN, WM_MBUTTONUP, (IntPtr)MK_MBUTTON),
        _ => throw new ArgumentException($"Unknown button: {button}"),
    };

    private static IntPtr MakeLParam(int x, int y) => new((y << 16) | (x & 0xFFFF));

    private static IntPtr ParseHwnd(string s)
    {
        var trimmed = s.StartsWith("0x", StringComparison.OrdinalIgnoreCase) ? s[2..] : s;
        return new IntPtr(Convert.ToInt64(trimmed, 16));
    }

    private static int Main(string[] args)
    {
        string? hwndArg = null;
        int? x = null;
        int? y = null;
        var button = "Left";

        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--hwnd": hwndArg = args[++i]; break;
                case "--x": x = int.Parse(args[++i]); break;
                case "--y": y = int.Parse(args[++i]); break;
                case "--button": button = args[++i]; break;
            }
        }

        if (hwndArg is null || x is null || y is null)
        {
            Console.Error.WriteLine("Missing required --hwnd, --x, --y");
            return 1;
        }

        try
        {
            var hwnd = ParseHwnd(hwndArg);
            var (down, up, wParamDown) = ButtonMessages(button);
            var lParam = MakeLParam(x.Value, y.Value);

            // PostMessage delivers straight to the target window's message queue regardless of
            // Z-order/foreground state — no SetCursorPos, no SetForegroundWindow, no real cursor
            // movement, so this never steals focus from whatever the user is actively doing.
            // Works for standard Win32 controls; GPU-rendered custom controls (Chromium/Electron/
            // games) may ignore posted messages and need a real click instead.
            PostMessage(hwnd, down, wParamDown, lParam);
            Thread.Sleep(20);
            PostMessage(hwnd, up, IntPtr.Zero, lParam);

            Console.WriteLine("OK");
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"windowmouse.exe failed: {ex.Message}");
            return 1;
        }
    }
}

using System.Runtime.InteropServices;

internal static class Program
{
    [DllImport("user32.dll")]
    private static extern bool SetProcessDPIAware();

    [DllImport("user32.dll")]
    private static extern bool SetCursorPos(int x, int y);

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out POINT lpPoint);

    [DllImport("user32.dll")]
    private static extern void mouse_event(uint dwFlags, int dx, int dy, int dwData, UIntPtr dwExtraInfo);

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT
    {
        public int X;
        public int Y;
    }

    private const uint LEFTDOWN = 0x0002;
    private const uint LEFTUP = 0x0004;
    private const uint RIGHTDOWN = 0x0008;
    private const uint RIGHTUP = 0x0010;
    private const uint MIDDLEDOWN = 0x0020;
    private const uint MIDDLEUP = 0x0040;
    private const uint WHEEL = 0x0800;

    private static uint ButtonFlag(string button, bool down) => button switch
    {
        "Left" => down ? LEFTDOWN : LEFTUP,
        "Right" => down ? RIGHTDOWN : RIGHTUP,
        "Middle" => down ? MIDDLEDOWN : MIDDLEUP,
        _ => throw new ArgumentException($"Unknown button: {button}"),
    };

    private static int Main(string[] args)
    {
        // Without this, the process is DPI-virtualized and SetCursorPos/GetCursorPos operate
        // in a scaled coordinate space that doesn't match real screen pixels (or the
        // DPI-aware coordinates returned by the screenshot server).
        SetProcessDPIAware();

        string? action = null;
        int? x = null;
        int? y = null;
        var button = "Left";
        var clicks = 1;
        var delta = 0;

        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--action": action = args[++i]; break;
                case "--x": x = int.Parse(args[++i]); break;
                case "--y": y = int.Parse(args[++i]); break;
                case "--button": button = args[++i]; break;
                case "--clicks": clicks = int.Parse(args[++i]); break;
                case "--delta": delta = int.Parse(args[++i]); break;
            }
        }

        if (action is null)
        {
            Console.Error.WriteLine("Missing required --action");
            return 1;
        }

        if ((action is "Move" or "Click" or "Down" or "Up") && x is not null && y is not null)
        {
            SetCursorPos(x.Value, y.Value);
        }

        switch (action)
        {
            case "Move":
            case "Position":
                break;
            case "Down":
                mouse_event(ButtonFlag(button, true), 0, 0, 0, UIntPtr.Zero);
                break;
            case "Up":
                mouse_event(ButtonFlag(button, false), 0, 0, 0, UIntPtr.Zero);
                break;
            case "Click":
                for (var i = 0; i < clicks; i++)
                {
                    mouse_event(ButtonFlag(button, true), 0, 0, 0, UIntPtr.Zero);
                    mouse_event(ButtonFlag(button, false), 0, 0, 0, UIntPtr.Zero);
                    if (i < clicks - 1) Thread.Sleep(50);
                }
                break;
            case "Scroll":
                mouse_event(WHEEL, 0, 0, delta * 120, UIntPtr.Zero);
                break;
            default:
                Console.Error.WriteLine($"Unknown action: {action}");
                return 1;
        }

        GetCursorPos(out var pos);
        Console.WriteLine($"{pos.X},{pos.Y}");
        return 0;
    }
}

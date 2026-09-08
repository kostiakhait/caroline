using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;

internal static class Program
{
    private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool EnumChildWindows(IntPtr hWndParent, EnumWindowsProc lpEnumFunc, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

    [DllImport("user32.dll")]
    private static extern bool GetClientRect(IntPtr hWnd, out RECT lpRect);

    [DllImport("user32.dll", CharSet = CharSet.Auto)]
    private static extern int GetClassName(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);

    [DllImport("user32.dll", CharSet = CharSet.Auto)]
    private static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

    [DllImport("user32.dll", CharSet = CharSet.Auto)]
    private static extern int GetWindowTextLength(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool IsWindowEnabled(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern IntPtr GetParent(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    // Only ever run on 64-bit Windows in practice (matches every other server in this repo),
    // so GetWindowLongPtr (not the 32-bit-only GetWindowLong) is safe to declare directly.
    [DllImport("user32.dll")]
    private static extern IntPtr GetWindowLongPtr(IntPtr hWnd, int nIndex);

    private const int GWLP_ID = -12;

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    private class RectDto
    {
        public int X { get; set; }
        public int Y { get; set; }
        public int Width { get; set; }
        public int Height { get; set; }
    }

    private class WindowRecord
    {
        public string Hwnd { get; set; } = "";
        public string Title { get; set; } = "";
        public string ClassName { get; set; } = "";
        public uint Pid { get; set; }
        public string? ProcessName { get; set; }
        public RectDto Rect { get; set; } = new();
        public RectDto ClientRect { get; set; } = new();
        public bool Visible { get; set; }
        public bool Enabled { get; set; }
        public long ControlId { get; set; }
        public string? ParentHwnd { get; set; }
    }

    private static string HwndToHex(IntPtr hwnd) => "0x" + hwnd.ToInt64().ToString("X8");

    private static IntPtr ParseHwnd(string s)
    {
        var trimmed = s.StartsWith("0x", StringComparison.OrdinalIgnoreCase) ? s[2..] : s;
        return new IntPtr(Convert.ToInt64(trimmed, 16));
    }

    private static string GetTitle(IntPtr hwnd)
    {
        var len = GetWindowTextLength(hwnd);
        if (len <= 0) return "";
        var sb = new StringBuilder(len + 1);
        GetWindowText(hwnd, sb, sb.Capacity);
        return sb.ToString();
    }

    private static string GetClass(IntPtr hwnd)
    {
        var sb = new StringBuilder(256);
        GetClassName(hwnd, sb, sb.Capacity);
        return sb.ToString();
    }

    private static RectDto ToRectDto(RECT r) => new()
    {
        X = r.Left,
        Y = r.Top,
        Width = r.Right - r.Left,
        Height = r.Bottom - r.Top,
    };

    private static WindowRecord Describe(IntPtr hwnd)
    {
        GetWindowRect(hwnd, out var rect);
        GetClientRect(hwnd, out var clientRect);
        GetWindowThreadProcessId(hwnd, out var pid);

        string? processName = null;
        try
        {
            processName = System.Diagnostics.Process.GetProcessById((int)pid).ProcessName;
        }
        catch
        {
            // Process may have exited between enumeration and lookup, or be inaccessible
            // (e.g. a protected/elevated process) — leave ProcessName null rather than fail.
        }

        var parent = GetParent(hwnd);

        return new WindowRecord
        {
            Hwnd = HwndToHex(hwnd),
            Title = GetTitle(hwnd),
            ClassName = GetClass(hwnd),
            Pid = pid,
            ProcessName = processName,
            Rect = ToRectDto(rect),
            ClientRect = ToRectDto(clientRect),
            Visible = IsWindowVisible(hwnd),
            Enabled = IsWindowEnabled(hwnd),
            ControlId = GetWindowLongPtr(hwnd, GWLP_ID).ToInt64(),
            ParentHwnd = parent == IntPtr.Zero ? null : HwndToHex(parent),
        };
    }

    private static bool MatchesFilters(
        WindowRecord w,
        string? titleFilter,
        string? classNameFilter,
        uint? pid,
        bool includeInvisible)
    {
        if (!includeInvisible && !w.Visible) return false;
        if (titleFilter is not null && w.Title.IndexOf(titleFilter, StringComparison.OrdinalIgnoreCase) < 0) return false;
        if (classNameFilter is not null && w.ClassName.IndexOf(classNameFilter, StringComparison.OrdinalIgnoreCase) < 0) return false;
        if (pid is not null && w.Pid != pid.Value) return false;
        return true;
    }

    private static int Main(string[] args)
    {
        string? action = null;
        string? hwndArg = null;
        string? titleFilter = null;
        string? classNameFilter = null;
        uint? pid = null;
        var includeInvisible = false;

        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--action": action = args[++i]; break;
                case "--hwnd": hwndArg = args[++i]; break;
                case "--titleFilter": titleFilter = args[++i]; break;
                case "--classNameFilter": classNameFilter = args[++i]; break;
                case "--pid": pid = uint.Parse(args[++i]); break;
                case "--includeInvisible": includeInvisible = bool.Parse(args[++i]); break;
            }
        }

        if (action is null)
        {
            Console.Error.WriteLine("Missing required --action");
            return 1;
        }

        var jsonOptions = new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase };

        try
        {
            switch (action)
            {
                case "list":
                {
                    var results = new List<WindowRecord>();
                    EnumWindows((hwnd, _) =>
                    {
                        var w = Describe(hwnd);
                        if (MatchesFilters(w, titleFilter, classNameFilter, pid, includeInvisible)) results.Add(w);
                        return true;
                    }, IntPtr.Zero);
                    Console.WriteLine(JsonSerializer.Serialize(results, jsonOptions));
                    return 0;
                }
                case "children":
                {
                    if (hwndArg is null)
                    {
                        Console.Error.WriteLine("Missing required --hwnd for --action children");
                        return 1;
                    }
                    var parentHwnd = ParseHwnd(hwndArg);
                    var results = new List<WindowRecord>();
                    EnumChildWindows(parentHwnd, (hwnd, _) =>
                    {
                        var w = Describe(hwnd);
                        if (MatchesFilters(w, titleFilter, classNameFilter, pid, includeInvisible)) results.Add(w);
                        return true;
                    }, IntPtr.Zero);
                    Console.WriteLine(JsonSerializer.Serialize(results, jsonOptions));
                    return 0;
                }
                case "info":
                {
                    if (hwndArg is null)
                    {
                        Console.Error.WriteLine("Missing required --hwnd for --action info");
                        return 1;
                    }
                    var hwnd = ParseHwnd(hwndArg);
                    Console.WriteLine(JsonSerializer.Serialize(Describe(hwnd), jsonOptions));
                    return 0;
                }
                default:
                    Console.Error.WriteLine($"Unknown action: {action}");
                    return 1;
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"inspect.exe failed: {ex.Message}");
            return 1;
        }
    }
}

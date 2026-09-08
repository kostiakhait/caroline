using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

internal static class Program
{
    // ---- user32/gdi32 P/Invoke: same declarations as the sibling servers in this repo ----
    // (mouse/native, keyboard/native, window-mouse/native, window-keyboard/native,
    // window-screenshot/native, inspect/native) - duplicated here rather than shared, matching
    // this repo's existing convention of each server owning its own native interop.

    [DllImport("user32.dll")] private static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] private static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] private static extern void mouse_event(uint dwFlags, int dx, int dy, int dwData, UIntPtr dwExtraInfo);
    [DllImport("user32.dll")] private static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] private static extern bool PrintWindow(IntPtr hWnd, IntPtr hdc, uint nFlags);
    private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll", CharSet = CharSet.Auto)] private static extern int GetClassName(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);
    [DllImport("user32.dll", CharSet = CharSet.Auto)] private static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll", CharSet = CharSet.Auto)] private static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    [DllImport("user32.dll")] private static extern IntPtr GetDC(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern int ReleaseDC(IntPtr hWnd, IntPtr hDC);
    [DllImport("gdi32.dll")] private static extern uint GetPixel(IntPtr hdc, int x, int y);
    [DllImport("user32.dll")] private static extern bool ClientToScreen(IntPtr hWnd, ref POINT lpPoint);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);

    private const uint PW_RENDERFULLCONTENT = 0x00000002;

    private const uint WM_MOUSEMOVE = 0x0200;
    private const uint WM_LBUTTONDOWN = 0x0201, WM_LBUTTONUP = 0x0202;
    private const uint WM_RBUTTONDOWN = 0x0204, WM_RBUTTONUP = 0x0205;
    private const uint WM_MBUTTONDOWN = 0x0207, WM_MBUTTONUP = 0x0208;
    private const uint WM_MOUSEWHEEL = 0x020A;
    private const uint WM_CHAR = 0x0102, WM_KEYDOWN = 0x0100, WM_KEYUP = 0x0101;
    private const int MK_LBUTTON = 0x0001, MK_RBUTTON = 0x0002, MK_MBUTTON = 0x0010;

    private const uint MOUSEEVENTF_LEFTDOWN = 0x0002, MOUSEEVENTF_LEFTUP = 0x0004;
    private const uint MOUSEEVENTF_RIGHTDOWN = 0x0008, MOUSEEVENTF_RIGHTUP = 0x0010;
    private const uint MOUSEEVENTF_MIDDLEDOWN = 0x0020, MOUSEEVENTF_MIDDLEUP = 0x0040;
    private const uint MOUSEEVENTF_WHEEL = 0x0800;

    private const uint INPUT_KEYBOARD = 1;
    private const uint KEYEVENTF_UNICODE = 0x0004, KEYEVENTF_KEYUP = 0x0002;

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT { public int Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT { public int X, Y; }

    [StructLayout(LayoutKind.Sequential)]
    private struct INPUT { public uint type; public InputUnion U; }

    [StructLayout(LayoutKind.Explicit, Size = 32)]
    private struct InputUnion { [FieldOffset(0)] public KEYBDINPUT ki; }

    [StructLayout(LayoutKind.Sequential)]
    private struct KEYBDINPUT { public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }

    // ---- step DTO: one shape covering every opcode's fields, camelCase-matched to the JSON the
    // TypeScript layer writes (src/index.ts's toWireStep) ----

    private class Step
    {
        public string Op { get; set; } = "";
        public string? Hwnd { get; set; }
        public int? X { get; set; }
        public int? Y { get; set; }
        public int? X1 { get; set; }
        public int? Y1 { get; set; }
        public int? X2 { get; set; }
        public int? Y2 { get; set; }
        public string? Button { get; set; }
        public int? Clicks { get; set; }
        public int? Steps { get; set; } // drag: intermediate move count
        public int? Vk { get; set; }
        public int[]? ModifierVks { get; set; }
        public string? Text { get; set; }
        public int? DelayMs { get; set; }
        public int? Delta { get; set; }
        public int? Ms { get; set; } // sleep
        public string? TitleFilter { get; set; }
        public string? ClassNameFilter { get; set; }
        public string? Pid { get; set; } // always a string on the wire: literal PID or "$name"
        public int? TimeoutMs { get; set; }
        public string? As { get; set; }
        public string? Color { get; set; }
        public int? Tolerance { get; set; }
        public int? Width { get; set; }
        public int? Height { get; set; }
        public int? StableMs { get; set; }
        public string? Path { get; set; }
        public string? Args { get; set; }
        public string? Cwd { get; set; }
        public string? ImageName { get; set; }
        public bool? All { get; set; }
        public bool? Force { get; set; }
        public int? Retries { get; set; }
        public int? RetryDelayMs { get; set; }
    }

    private class FailedAt
    {
        public int Index { get; set; }
        public string Op { get; set; } = "";
        public string Reason { get; set; } = "";
    }

    private class ChainResult
    {
        public string Status { get; set; } = "ok";
        public int CompletedSteps { get; set; }
        public FailedAt? FailedAt { get; set; }
        public long ElapsedMs { get; set; }
        public string? Screenshot { get; set; }
    }

    private class StopChainException : Exception
    {
        public string Status;
        public StopChainException(string status, string message) : base(message) { Status = status; }
    }

    // name -> pid, populated by launch/wait_window steps that carry "as"
    private static readonly Dictionary<string, int> PidBindings = new();

    private static IntPtr ParseHwnd(string s)
    {
        var trimmed = s.StartsWith("0x", StringComparison.OrdinalIgnoreCase) ? s[2..] : s;
        return new IntPtr(Convert.ToInt64(trimmed, 16));
    }

    // Resolves an hwnd field that may be a literal "0x..." handle or a "$name" bound by an
    // earlier launch/wait_window step. A PID isn't a window handle, so the "$name" case finds the
    // first visible top-level window currently owned by that PID.
    private static IntPtr ResolveHwnd(string hwndArg)
    {
        if (!hwndArg.StartsWith("$")) return ParseHwnd(hwndArg);

        var name = hwndArg[1..];
        if (!PidBindings.TryGetValue(name, out var pid))
        {
            throw new InvalidOperationException($"No binding named \"{name}\" (from an earlier launch/wait_window step's \"as\").");
        }

        IntPtr found = IntPtr.Zero;
        EnumWindows((hwnd, _) =>
        {
            GetWindowThreadProcessId(hwnd, out var ownerPid);
            if (ownerPid == (uint)pid && IsWindowVisible(hwnd))
            {
                found = hwnd;
                return false; // stop enumeration
            }
            return true;
        }, IntPtr.Zero);

        if (found == IntPtr.Zero)
        {
            throw new InvalidOperationException($"No visible top-level window currently owned by PID {pid} (bound to \"{name}\").");
        }
        return found;
    }

    private static int ResolvePid(string pidArg)
    {
        if (!pidArg.StartsWith("$")) return int.Parse(pidArg);
        if (!PidBindings.TryGetValue(pidArg[1..], out var pid))
        {
            throw new InvalidOperationException($"No binding named \"{pidArg[1..]}\" (from an earlier launch step's \"as\").");
        }
        return pid;
    }

    private static (uint down, uint up, IntPtr wParamDown) PostedButtonMessages(string button) => button switch
    {
        "Left" => (WM_LBUTTONDOWN, WM_LBUTTONUP, (IntPtr)MK_LBUTTON),
        "Right" => (WM_RBUTTONDOWN, WM_RBUTTONUP, (IntPtr)MK_RBUTTON),
        "Middle" => (WM_MBUTTONDOWN, WM_MBUTTONUP, (IntPtr)MK_MBUTTON),
        _ => throw new ArgumentException($"Unknown button: {button}"),
    };

    private static (uint down, uint up) GlobalButtonFlags(string button) => button switch
    {
        "Left" => (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "Right" => (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "Middle" => (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        _ => throw new ArgumentException($"Unknown button: {button}"),
    };

    private static IntPtr MakeLParam(int x, int y) => new((y << 16) | (x & 0xFFFF));

    // ---- keyboard primitives (mirrors keyboard/native and window-keyboard/native) ----

    private static void SendKeyInputs(INPUT[] inputs)
    {
        var sent = SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(INPUT)));
        if (sent != inputs.Length)
        {
            var error = Marshal.GetLastWin32Error();
            throw new InvalidOperationException($"SendInput inserted {sent}/{inputs.Length} events, GetLastError={error}");
        }
    }

    private static INPUT MakeUnicode(char ch, bool down) => new()
    {
        type = INPUT_KEYBOARD,
        U = new InputUnion { ki = new KEYBDINPUT { wVk = 0, wScan = ch, dwFlags = down ? KEYEVENTF_UNICODE : (KEYEVENTF_UNICODE | KEYEVENTF_KEYUP), time = 0, dwExtraInfo = IntPtr.Zero } }
    };

    private static INPUT MakeVk(int vk, bool down) => new()
    {
        type = INPUT_KEYBOARD,
        U = new InputUnion { ki = new KEYBDINPUT { wVk = (ushort)vk, wScan = 0, dwFlags = down ? 0u : KEYEVENTF_KEYUP, time = 0, dwExtraInfo = IntPtr.Zero } }
    };

    private static void GlobalType(string text, int delayMs)
    {
        foreach (var ch in text)
        {
            SendKeyInputs(new[] { MakeUnicode(ch, true), MakeUnicode(ch, false) });
            if (delayMs > 0) Thread.Sleep(delayMs);
        }
    }

    private static void GlobalPressCombo(int[] modifierVks, int vk)
    {
        var list = new List<INPUT>();
        foreach (var m in modifierVks) list.Add(MakeVk(m, true));
        list.Add(MakeVk(vk, true));
        list.Add(MakeVk(vk, false));
        for (var i = modifierVks.Length - 1; i >= 0; i--) list.Add(MakeVk(modifierVks[i], false));
        SendKeyInputs(list.ToArray());
    }

    private static void PostedType(IntPtr hwnd, string text, int delayMs)
    {
        foreach (var ch in text)
        {
            PostMessage(hwnd, WM_CHAR, (IntPtr)ch, IntPtr.Zero);
            if (delayMs > 0) Thread.Sleep(delayMs);
        }
    }

    private static void PostedPressCombo(IntPtr hwnd, int[] modifierVks, int vk)
    {
        foreach (var m in modifierVks) PostMessage(hwnd, WM_KEYDOWN, (IntPtr)m, IntPtr.Zero);
        PostMessage(hwnd, WM_KEYDOWN, (IntPtr)vk, IntPtr.Zero);
        PostMessage(hwnd, WM_KEYUP, (IntPtr)vk, IntPtr.Zero);
        for (var i = modifierVks.Length - 1; i >= 0; i--) PostMessage(hwnd, WM_KEYUP, (IntPtr)modifierVks[i], IntPtr.Zero);
    }

    // ---- capture primitives (mirrors window-screenshot/native and screenshot/native) ----

    private static Bitmap CaptureWindowBitmap(IntPtr hwnd)
    {
        if (!GetWindowRect(hwnd, out var rect))
        {
            throw new InvalidOperationException($"GetWindowRect failed for handle {hwnd} - is it a valid window?");
        }
        var width = rect.Right - rect.Left;
        var height = rect.Bottom - rect.Top;
        if (width <= 0 || height <= 0)
        {
            throw new InvalidOperationException($"Window has non-positive size ({width}x{height}) - it may be minimized.");
        }
        var bitmap = new Bitmap(width, height);
        using var graphics = Graphics.FromImage(bitmap);
        var hdc = graphics.GetHdc();
        try { PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT); }
        finally { graphics.ReleaseHdc(hdc); }
        return bitmap;
    }

    private static Bitmap CaptureRegionBitmap(int x, int y, int width, int height)
    {
        var bitmap = new Bitmap(width, height);
        using var graphics = Graphics.FromImage(bitmap);
        graphics.CopyFromScreen(x, y, 0, 0, new Size(width, height));
        return bitmap;
    }

    private static Bitmap CaptureFullScreenBitmap()
    {
        var bounds = System.Windows.Forms.SystemInformation.VirtualScreen;
        var bitmap = new Bitmap(bounds.Width, bounds.Height);
        using var graphics = Graphics.FromImage(bitmap);
        graphics.CopyFromScreen(bounds.Location, Point.Empty, bounds.Size);
        return bitmap;
    }

    private static string HashBitmap(Bitmap bitmap)
    {
        using var ms = new MemoryStream();
        bitmap.Save(ms, ImageFormat.Png);
        return Convert.ToHexString(MD5.HashData(ms.ToArray()));
    }

    // ---- window lookup (mirrors inspect/native) ----

    private static bool MatchesWindowFilter(IntPtr hwnd, string? titleFilter, string? classNameFilter, int? pid)
    {
        if (!IsWindowVisible(hwnd)) return false;

        if (titleFilter is not null)
        {
            var len = GetWindowTextLength(hwnd);
            var sb = new StringBuilder(len + 1);
            if (len > 0) GetWindowText(hwnd, sb, sb.Capacity);
            if (sb.ToString().IndexOf(titleFilter, StringComparison.OrdinalIgnoreCase) < 0) return false;
        }

        if (classNameFilter is not null)
        {
            var sb = new StringBuilder(256);
            GetClassName(hwnd, sb, sb.Capacity);
            if (sb.ToString().IndexOf(classNameFilter, StringComparison.OrdinalIgnoreCase) < 0) return false;
        }

        if (pid is not null)
        {
            GetWindowThreadProcessId(hwnd, out var ownerPid);
            if (ownerPid != (uint)pid.Value) return false;
        }

        return true;
    }

    private static IntPtr? FindWindow(string? titleFilter, string? classNameFilter, int? pid)
    {
        IntPtr found = IntPtr.Zero;
        EnumWindows((hwnd, _) =>
        {
            if (MatchesWindowFilter(hwnd, titleFilter, classNameFilter, pid))
            {
                found = hwnd;
                return false;
            }
            return true;
        }, IntPtr.Zero);
        return found == IntPtr.Zero ? null : found;
    }

    // ---- opcode execution ----
    // Each returns void and throws on failure; the caller (RunStep) applies retries/timeouts and
    // turns exceptions into a failed_at record.

    private static void ExecuteMoveClickDownUp(Step step)
    {
        var button = step.Button ?? "Left";
        if (step.Hwnd is not null)
        {
            var hwnd = ResolveHwnd(step.Hwnd);
            var lParam = MakeLParam(step.X!.Value, step.Y!.Value);
            var (postedDown, postedUp, postedWParamDown) = PostedButtonMessages(button);
            switch (step.Op)
            {
                case "move":
                    PostMessage(hwnd, WM_MOUSEMOVE, IntPtr.Zero, lParam);
                    break;
                case "mouse_down":
                    PostMessage(hwnd, postedDown, postedWParamDown, lParam);
                    break;
                case "mouse_up":
                    PostMessage(hwnd, postedUp, IntPtr.Zero, lParam);
                    break;
                case "click":
                    for (var i = 0; i < (step.Clicks ?? 1); i++)
                    {
                        PostMessage(hwnd, postedDown, postedWParamDown, lParam);
                        Thread.Sleep(20);
                        PostMessage(hwnd, postedUp, IntPtr.Zero, lParam);
                        if (i < (step.Clicks ?? 1) - 1) Thread.Sleep(50);
                    }
                    break;
            }
        }
        else
        {
            SetCursorPos(step.X!.Value, step.Y!.Value);
            var (down, up) = GlobalButtonFlags(button);
            switch (step.Op)
            {
                case "move":
                    break;
                case "mouse_down":
                    mouse_event(down, 0, 0, 0, UIntPtr.Zero);
                    break;
                case "mouse_up":
                    mouse_event(up, 0, 0, 0, UIntPtr.Zero);
                    break;
                case "click":
                    for (var i = 0; i < (step.Clicks ?? 1); i++)
                    {
                        mouse_event(down, 0, 0, 0, UIntPtr.Zero);
                        mouse_event(up, 0, 0, 0, UIntPtr.Zero);
                        if (i < (step.Clicks ?? 1) - 1) Thread.Sleep(50);
                    }
                    break;
            }
        }
    }

    private static void ExecuteDrag(Step step)
    {
        var button = step.Button ?? "Left";
        var steps = step.Steps ?? 10;

        // Real intermediate move events, not a single jump - a down->jump->up did not register
        // as a drag against UE5's Slate UI (global mode) in the session that motivated this
        // server; standard Win32 controls that track drag via posted WM_MOUSEMOVE with a
        // button-held wParam (local mode) have the same expectation.
        if (step.Hwnd is not null)
        {
            var hwnd = ResolveHwnd(step.Hwnd);
            var (down, up, wParamDown) = PostedButtonMessages(button);

            PostMessage(hwnd, down, wParamDown, MakeLParam(step.X1!.Value, step.Y1!.Value));
            Thread.Sleep(20);

            for (var i = 1; i <= steps; i++)
            {
                var t = (double)i / steps;
                var ix = (int)Math.Round(step.X1!.Value + (step.X2!.Value - step.X1!.Value) * t);
                var iy = (int)Math.Round(step.Y1!.Value + (step.Y2!.Value - step.Y1!.Value) * t);
                PostMessage(hwnd, WM_MOUSEMOVE, wParamDown, MakeLParam(ix, iy));
                Thread.Sleep(15);
            }

            PostMessage(hwnd, up, IntPtr.Zero, MakeLParam(step.X2!.Value, step.Y2!.Value));
        }
        else
        {
            var (down, up) = GlobalButtonFlags(button);

            SetCursorPos(step.X1!.Value, step.Y1!.Value);
            mouse_event(down, 0, 0, 0, UIntPtr.Zero);
            Thread.Sleep(20);

            for (var i = 1; i <= steps; i++)
            {
                var t = (double)i / steps;
                var ix = (int)Math.Round(step.X1!.Value + (step.X2!.Value - step.X1!.Value) * t);
                var iy = (int)Math.Round(step.Y1!.Value + (step.Y2!.Value - step.Y1!.Value) * t);
                SetCursorPos(ix, iy);
                Thread.Sleep(15);
            }

            mouse_event(up, 0, 0, 0, UIntPtr.Zero);
        }
    }

    private static void ExecuteKey(Step step)
    {
        var vk = step.Vk!.Value;
        var modVks = step.ModifierVks ?? Array.Empty<int>();
        if (step.Hwnd is not null) PostedPressCombo(ResolveHwnd(step.Hwnd), modVks, vk);
        else GlobalPressCombo(modVks, vk);
    }

    private static void ExecuteType(Step step)
    {
        var delay = step.DelayMs ?? 10;
        if (step.Hwnd is not null) PostedType(ResolveHwnd(step.Hwnd), step.Text ?? "", delay);
        else GlobalType(step.Text ?? "", delay);
    }

    private static void ExecuteScroll(Step step)
    {
        if (step.Hwnd is not null)
        {
            var hwnd = ResolveHwnd(step.Hwnd);
            // WM_MOUSEWHEEL is the one posted mouse message that takes SCREEN coordinates in
            // lParam, not client-relative - unlike WM_LBUTTONDOWN/WM_MOUSEMOVE/etc, per the Win32
            // spec. Convert the given client-relative point via ClientToScreen.
            var pt = new POINT { X = step.X ?? 0, Y = step.Y ?? 0 };
            ClientToScreen(hwnd, ref pt);
            var wParam = (IntPtr)(((step.Delta ?? 0) * 120) << 16);
            PostMessage(hwnd, WM_MOUSEWHEEL, wParam, MakeLParam(pt.X, pt.Y));
        }
        else
        {
            mouse_event(MOUSEEVENTF_WHEEL, 0, 0, (step.Delta ?? 0) * 120, UIntPtr.Zero);
        }
    }

    private static void ExecuteWaitWindow(Step step)
    {
        var pid = step.Pid is not null ? ResolvePid(step.Pid) : (int?)null;
        var sw = Stopwatch.StartNew();
        while (true)
        {
            var found = FindWindow(step.TitleFilter, step.ClassNameFilter, pid);
            if (found is not null)
            {
                if (step.As is not null)
                {
                    GetWindowThreadProcessId(found.Value, out var ownerPid);
                    PidBindings[step.As] = (int)ownerPid;
                }
                return;
            }
            if (sw.ElapsedMilliseconds >= step.TimeoutMs!.Value)
            {
                throw new TimeoutException($"wait_window timed out after {step.TimeoutMs}ms (titleFilter=\"{step.TitleFilter}\", classNameFilter=\"{step.ClassNameFilter}\", pid={pid})");
            }
            Thread.Sleep(100);
        }
    }

    private static void ExecuteWaitPixel(Step step)
    {
        var hex = step.Color!.TrimStart('#');
        var target = Color.FromArgb(
            Convert.ToInt32(hex[..2], 16),
            Convert.ToInt32(hex[2..4], 16),
            Convert.ToInt32(hex[4..6], 16));
        var tolerance = step.Tolerance ?? 0;

        bool Matches(int r, int g, int b) =>
            Math.Abs(r - target.R) <= tolerance && Math.Abs(g - target.G) <= tolerance && Math.Abs(b - target.B) <= tolerance;

        var sw = Stopwatch.StartNew();

        if (step.Hwnd is not null)
        {
            // Reads from a PrintWindow capture of the window's own content rather than the live
            // screen DC, so this works for a background/occluded window too - the same rationale
            // as window-screenshot's capture, not just "GetPixel but scoped to a rect."
            var hwnd = ResolveHwnd(step.Hwnd);
            (int r, int g, int b) last = default;
            while (true)
            {
                using (var bmp = CaptureWindowBitmap(hwnd))
                {
                    if (step.X!.Value >= 0 && step.Y!.Value >= 0 && step.X.Value < bmp.Width && step.Y.Value < bmp.Height)
                    {
                        var px = bmp.GetPixel(step.X.Value, step.Y.Value);
                        last = (px.R, px.G, px.B);
                        if (Matches(px.R, px.G, px.B)) return;
                    }
                }
                if (sw.ElapsedMilliseconds >= step.TimeoutMs!.Value)
                {
                    throw new TimeoutException($"wait_pixel timed out after {step.TimeoutMs}ms at window-relative ({step.X},{step.Y}), last saw #{last.r:X2}{last.g:X2}{last.b:X2}, wanted #{target.R:X2}{target.G:X2}{target.B:X2} +/-{tolerance}");
                }
                Thread.Sleep(100);
            }
        }
        else
        {
            var screenDc = GetDC(IntPtr.Zero);
            try
            {
                while (true)
                {
                    var raw = GetPixel(screenDc, step.X!.Value, step.Y!.Value);
                    var r = (int)(raw & 0x000000FF);
                    var g = (int)((raw & 0x0000FF00) >> 8);
                    var b = (int)((raw & 0x00FF0000) >> 16);

                    if (Matches(r, g, b)) return;
                    if (sw.ElapsedMilliseconds >= step.TimeoutMs!.Value)
                    {
                        throw new TimeoutException($"wait_pixel timed out after {step.TimeoutMs}ms at ({step.X},{step.Y}), last saw #{r:X2}{g:X2}{b:X2}, wanted #{target.R:X2}{target.G:X2}{target.B:X2} +/-{tolerance}");
                    }
                    Thread.Sleep(50);
                }
            }
            finally
            {
                ReleaseDC(IntPtr.Zero, screenDc);
            }
        }
    }

    private static void ExecuteWaitIdle(Step step)
    {
        Bitmap Capture() => step.Hwnd is not null
            ? CaptureWindowBitmap(ResolveHwnd(step.Hwnd))
            : CaptureRegionBitmap(step.X!.Value, step.Y!.Value, step.Width!.Value, step.Height!.Value);

        var sw = Stopwatch.StartNew();
        string? lastHash = null;
        long stableSince = sw.ElapsedMilliseconds;

        while (true)
        {
            string hash;
            using (var bmp = Capture()) hash = HashBitmap(bmp);

            if (hash != lastHash)
            {
                lastHash = hash;
                stableSince = sw.ElapsedMilliseconds;
            }
            else if (sw.ElapsedMilliseconds - stableSince >= step.StableMs!.Value)
            {
                return;
            }

            if (sw.ElapsedMilliseconds >= step.TimeoutMs!.Value)
            {
                throw new TimeoutException($"wait_idle timed out after {step.TimeoutMs}ms without {step.StableMs}ms of stable content");
            }
            Thread.Sleep(150);
        }
    }

    private static void ExecuteLaunch(Step step)
    {
        var psi = new ProcessStartInfo
        {
            FileName = step.Path ?? throw new InvalidOperationException("launch requires path"),
            Arguments = step.Args ?? "",
            WorkingDirectory = step.Cwd ?? "",
            UseShellExecute = true,
        };
        var process = Process.Start(psi) ?? throw new InvalidOperationException($"Process.Start returned null for \"{step.Path}\"");
        if (step.As is not null) PidBindings[step.As] = process.Id;
    }

    private static void ExecuteKill(Step step)
    {
        var force = step.Force ?? false;

        if (step.ImageName is not null && (step.All ?? false))
        {
            foreach (var proc in Process.GetProcessesByName(step.ImageName))
            {
                try { proc.Kill(force); } catch { /* already exited */ }
            }
            return;
        }

        if (step.Pid is not null)
        {
            var pid = ResolvePid(step.Pid);
            Process.GetProcessById(pid).Kill(force);
            return;
        }

        throw new InvalidOperationException("kill requires either pid or imageName+all:true");
    }

    private static void ExecuteRestart(Step step)
    {
        ExecuteKill(step);
        Thread.Sleep(200);
        ExecuteLaunch(step);
    }

    private static string ExecuteCheckpoint(Step step, string? outFile)
    {
        if (outFile is null) return "";
        using var bitmap = step.Hwnd is not null ? CaptureWindowBitmap(ResolveHwnd(step.Hwnd)) : CaptureFullScreenBitmap();
        bitmap.Save(outFile, ImageFormat.Png);
        return outFile;
    }

    // ---- main interpreter loop ----

    private static readonly HashSet<string> RetryEligible = new() { "move", "click", "mouse_down", "mouse_up", "key" };

    private static void RunStep(Step step)
    {
        var retries = RetryEligible.Contains(step.Op) ? (step.Retries ?? 0) : 0;
        var retryDelay = step.RetryDelayMs ?? 200;

        for (var attempt = 0; ; attempt++)
        {
            try
            {
                switch (step.Op)
                {
                    case "move": case "click": case "mouse_down": case "mouse_up":
                        ExecuteMoveClickDownUp(step); return;
                    case "drag": ExecuteDrag(step); return;
                    case "key": ExecuteKey(step); return;
                    case "type": ExecuteType(step); return;
                    case "scroll": ExecuteScroll(step); return;
                    case "sleep": Thread.Sleep(step.Ms!.Value); return;
                    case "wait_window": ExecuteWaitWindow(step); return;
                    case "wait_pixel": ExecuteWaitPixel(step); return;
                    case "wait_idle": ExecuteWaitIdle(step); return;
                    case "launch": ExecuteLaunch(step); return;
                    case "kill": ExecuteKill(step); return;
                    case "restart": ExecuteRestart(step); return;
                    default: throw new InvalidOperationException($"Unknown op: {step.Op}");
                }
            }
            catch (Exception) when (attempt < retries)
            {
                Thread.Sleep(retryDelay);
            }
        }
    }

    private static int Main(string[] args)
    {
        SetProcessDPIAware();

        string? stepsFile = null;
        string? outFile = null;
        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--stepsFile": stepsFile = args[++i]; break;
                case "--out": outFile = args[++i]; break;
            }
        }
        if (stepsFile is null)
        {
            Console.Error.WriteLine("Missing required --stepsFile");
            return 1;
        }

        var jsonOptions = new JsonSerializerOptions
        {
            PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
            PropertyNameCaseInsensitive = true,
        };

        List<Step> steps;
        try
        {
            steps = JsonSerializer.Deserialize<List<Step>>(File.ReadAllText(stepsFile), jsonOptions)
                ?? throw new InvalidOperationException("steps file deserialized to null");
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"chain.exe failed to read steps file: {ex.Message}");
            return 1;
        }

        var sw = Stopwatch.StartNew();
        var result = new ChainResult();

        for (var i = 0; i < steps.Count; i++)
        {
            var step = steps[i];

            if (step.Op == "checkpoint")
            {
                try
                {
                    result.Screenshot = ExecuteCheckpoint(step, outFile) is { Length: > 0 } p ? p : null;
                }
                catch
                {
                    result.Screenshot = null; // best-effort - a failed checkpoint screenshot shouldn't hide the pause itself
                }
                result.Status = "paused";
                result.CompletedSteps = i + 1;
                break;
            }

            try
            {
                RunStep(step);
                result.CompletedSteps = i + 1;
            }
            catch (Exception ex)
            {
                result.Status = "failed";
                result.CompletedSteps = i;
                result.FailedAt = new FailedAt { Index = i, Op = step.Op, Reason = ex.Message };
                try
                {
                    if (outFile is not null)
                    {
                        using var bmp = CaptureFullScreenBitmap();
                        bmp.Save(outFile, ImageFormat.Png);
                        result.Screenshot = outFile;
                    }
                }
                catch { /* best-effort */ }
                break;
            }
        }

        if (result.Status == "ok" && result.CompletedSteps == steps.Count)
        {
            // Ran every step without a checkpoint/failure - status stays "ok" (its default).
        }

        result.ElapsedMs = sw.ElapsedMilliseconds;

        Console.WriteLine(JsonSerializer.Serialize(result, jsonOptions));
        return 0;
    }
}

using System.Runtime.InteropServices;

internal static class Program
{
    [DllImport("user32.dll")]
    private static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);

    private const uint WM_CHAR = 0x0102;
    private const uint WM_KEYDOWN = 0x0100;
    private const uint WM_KEYUP = 0x0101;

    private static IntPtr ParseHwnd(string s)
    {
        var trimmed = s.StartsWith("0x", StringComparison.OrdinalIgnoreCase) ? s[2..] : s;
        return new IntPtr(Convert.ToInt64(trimmed, 16));
    }

    private static void PostChar(IntPtr hwnd, char ch)
    {
        PostMessage(hwnd, WM_CHAR, (IntPtr)ch, IntPtr.Zero);
    }

    private static void PostKey(IntPtr hwnd, int vk, bool down)
    {
        PostMessage(hwnd, down ? WM_KEYDOWN : WM_KEYUP, (IntPtr)vk, IntPtr.Zero);
    }

    private static int Main(string[] args)
    {
        string? action = null;
        string? hwndArg = null;
        string? text = null;
        var delayMs = 10;
        int? vk = null;
        var modifiers = new List<int>();

        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--action": action = args[++i]; break;
                case "--hwnd": hwndArg = args[++i]; break;
                case "--text": text = args[++i]; break;
                case "--delayms": delayMs = int.Parse(args[++i]); break;
                case "--vk": vk = int.Parse(args[++i]); break;
                case "--modifiers":
                    var raw = args[++i];
                    if (raw.Length > 0)
                    {
                        foreach (var part in raw.Split(','))
                            modifiers.Add(int.Parse(part));
                    }
                    break;
            }
        }

        if (action is null || hwndArg is null)
        {
            Console.Error.WriteLine("Missing required --action, --hwnd");
            return 1;
        }
        var hwnd = ParseHwnd(hwndArg);

        try
        {
            switch (action)
            {
                case "text":
                {
                    if (text is null)
                    {
                        Console.Error.WriteLine("Missing required --text for --action text");
                        return 1;
                    }
                    // Posted directly to the target window's queue as WM_CHAR - no SetFocus, no
                    // SendInput, no global keyboard-state change, so this never steals focus.
                    // Works for standard Win32 edit/static controls; GPU-rendered custom text
                    // inputs (Chromium/Electron) may ignore posted WM_CHAR.
                    foreach (var ch in text)
                    {
                        PostChar(hwnd, ch);
                        if (delayMs > 0) Thread.Sleep(delayMs);
                    }
                    Console.WriteLine("OK");
                    return 0;
                }
                case "key":
                {
                    if (vk is null)
                    {
                        Console.Error.WriteLine("Missing required --vk for --action key");
                        return 1;
                    }
                    // Modifiers down (in given order), then the key itself, then modifiers up in
                    // reverse order - mirrors windows-keyboard's PressCombo, but posted messages
                    // instead of SendInput. Background modifier-combo fidelity isn't guaranteed
                    // against every app (some read live modifier key state instead of trusting
                    // posted WM_KEYDOWN) - fine for the primary case of a plain key/text into a
                    // found control.
                    foreach (var m in modifiers) PostKey(hwnd, m, true);
                    PostKey(hwnd, vk.Value, true);
                    PostKey(hwnd, vk.Value, false);
                    for (var i = modifiers.Count - 1; i >= 0; i--) PostKey(hwnd, modifiers[i], false);

                    Console.WriteLine("OK");
                    return 0;
                }
                default:
                    Console.Error.WriteLine($"Unknown action: {action}");
                    return 1;
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"windowkeyboard.exe failed: {ex.Message}");
            return 1;
        }
    }
}

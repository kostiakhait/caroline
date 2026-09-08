using System.Runtime.InteropServices;

internal static class Program
{
    private const uint INPUT_KEYBOARD = 1;
    private const uint KEYEVENTF_UNICODE = 0x0004;
    private const uint KEYEVENTF_KEYUP = 0x0002;

    [StructLayout(LayoutKind.Sequential)]
    private struct INPUT
    {
        public uint type;
        public InputUnion U;
    }

    // Explicit Size = 32 (x64) matches the real Win32 INPUT union, which must be big enough
    // for MOUSEINPUT (the largest member) even though we only ever populate `ki`. Without
    // this, Marshal.SizeOf<INPUT>() undercounts the struct, SendInput's cbSize check fails,
    // and the call silently inserts zero events. See DictateWin/src/Interop/NativeMethods.cs.
    [StructLayout(LayoutKind.Explicit, Size = 32)]
    private struct InputUnion
    {
        [FieldOffset(0)] public KEYBDINPUT ki;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct KEYBDINPUT
    {
        public ushort wVk;
        public ushort wScan;
        public uint dwFlags;
        public uint time;
        public IntPtr dwExtraInfo;
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);

    private static INPUT MakeUnicode(char ch, bool down) => new()
    {
        type = INPUT_KEYBOARD,
        U = new InputUnion
        {
            ki = new KEYBDINPUT
            {
                wVk = 0,
                wScan = ch,
                dwFlags = down ? KEYEVENTF_UNICODE : (KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
                time = 0,
                dwExtraInfo = IntPtr.Zero,
            }
        }
    };

    private static INPUT MakeVk(int vk, bool down) => new()
    {
        type = INPUT_KEYBOARD,
        U = new InputUnion
        {
            ki = new KEYBDINPUT
            {
                wVk = (ushort)vk,
                wScan = 0,
                dwFlags = down ? 0u : KEYEVENTF_KEYUP,
                time = 0,
                dwExtraInfo = IntPtr.Zero,
            }
        }
    };

    private static void Send(INPUT[] inputs)
    {
        var sent = SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(INPUT)));
        if (sent != inputs.Length)
        {
            var error = Marshal.GetLastWin32Error();
            throw new InvalidOperationException($"SendInput inserted {sent}/{inputs.Length} events, GetLastError={error}");
        }
    }

    private static void TypeUnicodeChar(char ch) => Send(new[] { MakeUnicode(ch, true), MakeUnicode(ch, false) });

    private static void PressCombo(int[] modifierVks, int vk)
    {
        var list = new List<INPUT>();
        foreach (var m in modifierVks) list.Add(MakeVk(m, true));
        list.Add(MakeVk(vk, true));
        list.Add(MakeVk(vk, false));
        for (var i = modifierVks.Length - 1; i >= 0; i--) list.Add(MakeVk(modifierVks[i], false));
        Send(list.ToArray());
    }

    private static void KeyDown(int vk) => Send(new[] { MakeVk(vk, true) });

    private static void KeyUp(int vk) => Send(new[] { MakeVk(vk, false) });

    private static int Main(string[] args)
    {
        string? action = null;
        string? text = null;
        var delayMs = 10;
        var vk = 0;
        var modifierVks = Array.Empty<int>();

        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--action": action = args[++i]; break;
                case "--text": text = args[++i]; break;
                case "--delayms": delayMs = int.Parse(args[++i]); break;
                case "--vk": vk = int.Parse(args[++i]); break;
                case "--modifiers":
                    var raw = args[++i];
                    modifierVks = raw.Length == 0
                        ? Array.Empty<int>()
                        : raw.Split(',').Select(int.Parse).ToArray();
                    break;
            }
        }

        switch (action)
        {
            case "Type":
                foreach (var ch in text ?? string.Empty)
                {
                    TypeUnicodeChar(ch);
                    if (delayMs > 0) Thread.Sleep(delayMs);
                }
                break;
            case "Press":
                PressCombo(modifierVks, vk);
                break;
            case "Down":
                KeyDown(vk);
                break;
            case "Up":
                KeyUp(vk);
                break;
            default:
                Console.Error.WriteLine($"Unknown action: {action}");
                return 1;
        }

        Console.WriteLine("OK");
        return 0;
    }
}

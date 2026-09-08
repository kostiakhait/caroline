using System.Runtime.InteropServices;

namespace Caroline.Native;

/// <summary>
/// Real, OS-level mouse/keyboard input (SetCursorPos + mouse_event for clicks,
/// SendInput with KEYEVENTF_UNICODE for typing, VK-code SendInput for named
/// keys) -- the same technique MCP/mouse and MCP/keyboard's native helpers
/// already use, ported in here directly rather than shelling out to those
/// separate .exe files (which live in backend/mcp-servers/, not this WPF
/// project's own output folder).
///
/// Why this exists at all: confirmed live (2026-08-31) that AppBrowserWindow's
/// JS-based click/type (dispatchEvent/execCommand) produces events with
/// isTrusted=false, and modern web apps (WhatsApp Web among them) silently
/// ignore untrusted input on protected actions -- a real OS-level click/
/// keystroke is indistinguishable from a human at the browser's own input
/// layer, so it works where JS dispatch doesn't. Trade-off, same as MCP/mouse:
/// this physically moves the real system cursor and requires the target
/// window to actually be focused/on-screen/unobscured -- not free of side
/// effects the way JS-dispatch is.
/// </summary>
internal static class RealInput
{
    [DllImport("user32.dll")] private static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] private static extern void mouse_event(uint dwFlags, int dx, int dy, int dwData, UIntPtr dwExtraInfo);

    private const uint LEFTDOWN = 0x0002;
    private const uint LEFTUP = 0x0004;
    private const uint WHEEL = 0x0800;
    private const int WHEEL_DELTA = 120; // one notch, per the Win32 convention MCP/mouse's own Program.cs already uses

    /// <summary>Real click at absolute screen coordinates (physical pixels -- see
    /// AppBrowserWindow's callers for the DIP-to-physical conversion).</summary>
    public static void Click(int screenX, int screenY)
    {
        SetCursorPos(screenX, screenY);
        mouse_event(LEFTDOWN, 0, 0, 0, UIntPtr.Zero);
        Thread.Sleep(30);
        mouse_event(LEFTUP, 0, 0, 0, UIntPtr.Zero);
    }

    /// <summary>
    /// Real mouse-wheel scroll at absolute screen coordinates -- targets
    /// whatever's actually under that point (the OS routes wheel events by
    /// cursor position, not focus), so this can scroll a specific inner
    /// element (a message list, say) without the ambiguity of a page-level
    /// PageUp/PageDown when focus in a SPA has wandered somewhere else.
    /// `clicks` is signed: positive scrolls up (content moves down), negative
    /// scrolls down -- same sign convention as a physical wheel notch.
    /// </summary>
    public static void Scroll(int screenX, int screenY, int clicks)
    {
        SetCursorPos(screenX, screenY);
        mouse_event(WHEEL, 0, 0, clicks * WHEEL_DELTA, UIntPtr.Zero);
    }

    private const uint INPUT_KEYBOARD = 1;
    private const uint KEYEVENTF_UNICODE = 0x0004;
    private const uint KEYEVENTF_KEYUP = 0x0002;

    [StructLayout(LayoutKind.Sequential)]
    private struct INPUT
    {
        public uint type;
        public InputUnion U;
    }

    // Explicit Size = 32 (x64) -- must match the real Win32 INPUT union (big enough for
    // MOUSEINPUT, the largest member) even though only `ki` is ever populated here. Without
    // this, Marshal.SizeOf<INPUT>() undercounts the struct and SendInput's cbSize check fails,
    // silently inserting zero events (same gotcha documented in MCP/keyboard's Program.cs).
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
        U = new InputUnion { ki = new KEYBDINPUT { wVk = 0, wScan = ch, dwFlags = down ? KEYEVENTF_UNICODE : (KEYEVENTF_UNICODE | KEYEVENTF_KEYUP), time = 0, dwExtraInfo = IntPtr.Zero } },
    };

    private static INPUT MakeVk(int vk, bool down) => new()
    {
        type = INPUT_KEYBOARD,
        U = new InputUnion { ki = new KEYBDINPUT { wVk = (ushort)vk, wScan = 0, dwFlags = down ? 0u : KEYEVENTF_KEYUP, time = 0, dwExtraInfo = IntPtr.Zero } },
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

    /// <summary>Real keystrokes for arbitrary Unicode text -- works for any character,
    /// not just what a physical keyboard layout can produce directly.</summary>
    public static void TypeUnicode(string text, int delayMs = 8)
    {
        foreach (var ch in text)
        {
            Send(new[] { MakeUnicode(ch, true), MakeUnicode(ch, false) });
            if (delayMs > 0) Thread.Sleep(delayMs);
        }
    }

    private static readonly Dictionary<string, int> NamedKeys = new(StringComparer.OrdinalIgnoreCase)
    {
        ["enter"] = 0x0D, ["return"] = 0x0D, ["escape"] = 0x1B, ["esc"] = 0x1B, ["tab"] = 0x09,
        ["backspace"] = 0x08, ["space"] = 0x20, ["spacebar"] = 0x20, ["capslock"] = 0x14,
        ["left"] = 0x25, ["up"] = 0x26, ["right"] = 0x27, ["down"] = 0x28, ["home"] = 0x24, ["end"] = 0x23,
        ["pageup"] = 0x21, ["pagedown"] = 0x22, ["insert"] = 0x2D, ["delete"] = 0x2E, ["del"] = 0x2E,
        ["ctrl"] = 0x11, ["control"] = 0x11, ["shift"] = 0x10, ["alt"] = 0x12, ["menu"] = 0x12,
        ["win"] = 0x5B, ["windows"] = 0x5B,
    };

    static RealInput()
    {
        for (var i = 1; i <= 24; i++) NamedKeys[$"f{i}"] = 0x6F + i;
    }

    private static int ResolveVk(string key)
    {
        var normalized = key.Trim();
        if (NamedKeys.TryGetValue(normalized, out var vk)) return vk;
        if (normalized.Length == 1)
        {
            var ch = char.ToUpperInvariant(normalized[0]);
            if (ch is >= 'A' and <= 'Z' or >= '0' and <= '9') return ch;
        }
        throw new ArgumentException($"Unknown key name: \"{key}\"");
    }

    /// <summary>Presses a key, optionally combined with modifiers (e.g. "Control+A" -- see
    /// ParseCombo). Real SendInput, same trust level as a physical keystroke.</summary>
    public static void PressCombo(string keyExpression)
    {
        var parts = keyExpression.Split('+', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        if (parts.Length == 0) throw new ArgumentException("Empty key expression.");
        var modifierVks = parts[..^1].Select(ResolveVk).ToArray();
        var vk = ResolveVk(parts[^1]);

        var list = new List<INPUT>();
        foreach (var m in modifierVks) list.Add(MakeVk(m, true));
        list.Add(MakeVk(vk, true));
        list.Add(MakeVk(vk, false));
        for (var i = modifierVks.Length - 1; i >= 0; i--) list.Add(MakeVk(modifierVks[i], false));
        Send(list.ToArray());
    }
}

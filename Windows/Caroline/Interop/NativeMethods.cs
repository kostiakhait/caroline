using System.Runtime.InteropServices;

namespace Caroline.Interop;

internal static class NativeMethods
{
    public const int WM_HOTKEY = 0x0312;

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool RegisterHotKey(nint hWnd, int id, uint fsModifiers, uint vk);

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool UnregisterHotKey(nint hWnd, int id);

    // WPF's Window.Activate() can silently no-op: Windows' foreground-lock
    // rules block a background/newly-launched process from stealing focus
    // from whatever else is in front (e.g. the installer window that just
    // closed itself right before spawning Caroline) -- SetForegroundWindow
    // is the same call Activate() uses internally, but calling it directly
    // after a short delay (once the installer's own window has actually
    // gone away) succeeds where the immediate Activate() didn't.
    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool SetForegroundWindow(nint hWnd);
}

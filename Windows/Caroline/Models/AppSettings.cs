namespace Caroline.Models;

public class AppSettings
{
    public bool AutoStart { get; set; } = false;

    // Per explicit instruction (2026-09-04): the chat window is topmost by default
    // (stays above other windows) -- toggleable in Settings for anyone who finds
    // that intrusive.
    public bool AlwaysOnTop { get; set; } = true;

    // Default global hotkey: Ctrl+Alt+C. Stored as raw Win32 modifier/VK
    // codes (see Interop.NativeMethods) so they round-trip through JSON
    // without needing a separate parser for a human-readable form yet.
    public uint HotkeyModifiers { get; set; } = NativeModifiers.Control | NativeModifiers.Alt;
    public uint HotkeyVirtualKey { get; set; } = 0x43; // 'C'

    // NaN means "never customized/saved yet" for all four -- MainWindow
    // computes a real default from the screen's work area in that case
    // (pinned to the right edge, 20% of screen width, full available
    // height) instead of a fixed pixel size that wouldn't scale sensibly
    // across different monitors. Once the user moves/resizes the window
    // even once, all four get saved together (see MainWindow's own save
    // point) and these NaN defaults never apply again.
    public double WindowLeft { get; set; } = double.NaN;
    public double WindowTop { get; set; } = double.NaN;
    public double WindowWidth { get; set; } = double.NaN;
    public double WindowHeight { get; set; } = double.NaN;

    // Which chat tabs were open last time (see MainWindow's tab strip), so
    // relaunching the app reopens the same ones -- each resumes its own
    // conversation via the backend's per-tab resume session id (server.ts),
    // not just a blank new tab. "1" is the primary tab id (must match
    // server.ts's PRIMARY_TAB_ID) -- also what a pre-multi-tab install
    // implicitly had, so upgrading lands on the tab that inherits the old
    // single conversation (see durability.ts's migration fallback).
    public List<string> OpenTabIds { get; set; } = new() { "1" };

    // User-assigned display names for tabs (see MainWindow's rename-in-place
    // UI), keyed by tab id. A tab with no entry here just shows "Tab {id}"
    // (MainWindow.TabDisplayName's fallback) -- most tabs never get renamed.
    public Dictionary<string, string> TabNames { get; set; } = new();
}

public static class NativeModifiers
{
    public const uint Alt = 0x0001;
    public const uint Control = 0x0002;
    public const uint Shift = 0x0004;
    public const uint Win = 0x0008;
}

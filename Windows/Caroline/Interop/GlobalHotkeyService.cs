using System.Collections.Generic;
using System.Windows;
using System.Windows.Interop;

namespace Caroline.Interop;

/// <summary>
/// Registers a single system-wide hotkey against a WPF window's message loop
/// (WM_HOTKEY), independent of whether that window has focus. Must be
/// created after the window's handle exists (call from OnSourceInitialized).
/// </summary>
public sealed class GlobalHotkeyService : IDisposable
{
    // Arbitrary base, just needs to not collide with any other WM_HOTKEY
    // consumer in this process -- each registered hotkey gets its own id
    // (BaseHotkeyId + index) since RegisterHotKey is keyed by (hwnd, id).
    private const int BaseHotkeyId = 0xCA30;
    private readonly HwndSource _source;
    private readonly List<(int id, bool registered)> _slots = new();

    /// <summary>Raised with the same index passed to Register, so one service can drive several distinct hotkeys.</summary>
    public event Action<int>? HotkeyPressed;

    public GlobalHotkeyService(Window window)
    {
        var handle = new WindowInteropHelper(window).Handle;
        _source = HwndSource.FromHwnd(handle)
            ?? throw new InvalidOperationException("Window handle not available yet -- call from OnSourceInitialized.");
        _source.AddHook(WndProc);
    }

    /// <summary>Registers one hotkey under the given index (0, 1, 2, ...); re-registering the same index replaces it.</summary>
    public bool Register(int index, uint modifiers, uint virtualKey)
    {
        Unregister(index);
        while (_slots.Count <= index) _slots.Add((BaseHotkeyId + _slots.Count, false));
        var id = _slots[index].id;
        var ok = NativeMethods.RegisterHotKey(_source.Handle, id, modifiers, virtualKey);
        _slots[index] = (id, ok);
        return ok;
    }

    public void Unregister(int index)
    {
        if (index >= _slots.Count || !_slots[index].registered) return;
        NativeMethods.UnregisterHotKey(_source.Handle, _slots[index].id);
        _slots[index] = (_slots[index].id, false);
    }

    private nint WndProc(nint hwnd, int msg, nint wParam, nint lParam, ref bool handled)
    {
        if (msg == NativeMethods.WM_HOTKEY)
        {
            var pressedId = wParam.ToInt32();
            for (var i = 0; i < _slots.Count; i++)
            {
                if (_slots[i].id != pressedId) continue;
                HotkeyPressed?.Invoke(i);
                handled = true;
                break;
            }
        }
        return nint.Zero;
    }

    public void Dispose()
    {
        for (var i = 0; i < _slots.Count; i++) Unregister(i);
        _source.RemoveHook(WndProc);
    }
}

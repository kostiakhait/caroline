using System.Runtime.InteropServices;
using System.Text;
using Caroline.Services;

namespace Caroline.Native;

/// <summary>
/// Collapses the native Windows "Open"/"Save As" file-picker dance into one
/// call -- confirmed live (2026-08-31) that driving one by hand (find the
/// dialog's HWND, find its filename Edit control, type the path, press
/// Enter) took 4 separate tool calls with a real race if the dialog hadn't
/// finished appearing yet. This waits for the dialog itself, so callers
/// don't need their own retry loop for that.
/// </summary>
internal static class FileDialogHelper
{
    private const string DialogClassName = "#32770"; // standard Win32 common-dialog window class

    [DllImport("user32.dll")] private static extern IntPtr FindWindow(string? lpClassName, string? lpWindowName);
    [DllImport("user32.dll")] private static extern IntPtr FindWindowEx(IntPtr hwndParent, IntPtr hwndChildAfter, string? lpszClass, string? lpszWindow);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern int GetClassName(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll", CharSet = CharSet.Auto)] private static extern IntPtr SendMessage(IntPtr hWnd, uint msg, IntPtr wParam, string lParam);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    private const uint WM_SETTEXT = 0x000C;

    private static IntPtr FindDialogWindow()
    {
        IntPtr found = IntPtr.Zero;
        EnumWindows((hWnd, _) =>
        {
            if (!IsWindowVisible(hWnd)) return true;
            var sb = new StringBuilder(256);
            GetClassName(hWnd, sb, sb.Capacity);
            if (sb.ToString() == DialogClassName)
            {
                found = hWnd; // last match wins -- the most recently enumerated (topmost Z-order first, per EnumWindows' own order) is the one we want if several exist
                return false;
            }
            return true;
        }, IntPtr.Zero);
        return found;
    }

    /// <summary>Walks the dialog's own child tree for the filename Edit control -- it's
    /// nested under a ComboBoxEx32/ComboBox in modern (Vista+) common dialogs.</summary>
    private static IntPtr FindFilenameEdit(IntPtr dialogHwnd)
    {
        // Modern layout: dialog -> ComboBoxEx32 (id 1148, "File name:") -> ComboBox -> Edit.
        var comboEx = FindWindowEx(dialogHwnd, IntPtr.Zero, "ComboBoxEx32", null);
        if (comboEx != IntPtr.Zero)
        {
            var combo = FindWindowEx(comboEx, IntPtr.Zero, "ComboBox", null);
            if (combo != IntPtr.Zero)
            {
                var edit = FindWindowEx(combo, IntPtr.Zero, "Edit", null);
                if (edit != IntPtr.Zero) return edit;
            }
        }
        // Fallback: a plain Edit directly under the dialog (older-style dialogs).
        return FindWindowEx(dialogHwnd, IntPtr.Zero, "Edit", null);
    }

    /// <summary>
    /// Waits up to `timeoutMs` for a native file-picker dialog to appear, types the given
    /// path(s) into its filename field, and confirms (Enter). Multiple paths, space-separated
    /// and each individually double-quoted, select multiple files in one go -- same convention
    /// Explorer's own file-open dialog accepts.
    /// </summary>
    public static async Task<string> FillAndConfirmAsync(string[] paths, int timeoutMs = 10_000)
    {
        Logger.Log($"[file-dialog-helper] FillAndConfirmAsync: waiting for dialog (paths={string.Join(", ", paths)}, timeoutMs={timeoutMs})");
        var deadline = DateTime.UtcNow.AddMilliseconds(timeoutMs);
        IntPtr dialogHwnd;
        while ((dialogHwnd = FindDialogWindow()) == IntPtr.Zero)
        {
            if (DateTime.UtcNow > deadline)
            {
                Logger.Log("[file-dialog-helper] FillAndConfirmAsync: timed out waiting for a dialog to appear");
                return "no-dialog-found";
            }
            await Task.Delay(150);
        }
        Logger.Log($"[file-dialog-helper] FillAndConfirmAsync: found dialog hwnd={dialogHwnd}");
        SetForegroundWindow(dialogHwnd);
        await Task.Delay(100); // let foreground activation settle

        var edit = FindFilenameEdit(dialogHwnd);
        if (edit == IntPtr.Zero)
        {
            Logger.Log("[file-dialog-helper] FillAndConfirmAsync: dialog found, but no filename Edit control located");
            return "no-edit-control-found";
        }
        Logger.Log($"[file-dialog-helper] FillAndConfirmAsync: filename Edit control hwnd={edit}");

        var text = paths.Length == 1 ? paths[0] : string.Join(" ", paths.Select(p => $"\"{p}\""));
        SendMessage(edit, WM_SETTEXT, IntPtr.Zero, text);
        await Task.Delay(100);
        RealInput.PressCombo("Enter");
        Logger.Log("[file-dialog-helper] FillAndConfirmAsync: text set and Enter pressed");
        return "confirmed";
    }
}

using System.Drawing;
using System.Windows.Forms;
using Caroline.Services;

namespace Caroline.Tray;

public class TrayIconManager : IDisposable
{
    private readonly NotifyIcon _notifyIcon;
    private readonly ContextMenuStrip _contextMenu;
    private readonly ToolStripItem _openItem;
    private ToolStripItem? _updateItem;

    public event EventHandler? OpenRequested;
    public event EventHandler? ExitRequested;
    /// <summary>Fired when the user clicks the "Update to ..." item -- unlike the once-per-launch
    /// MessageBox prompt (see UpdateChecker), this stays available indefinitely after a declined
    /// prompt, so a later manual update doesn't require restarting the app first.</summary>
    public event EventHandler? UpdateRequested;

    public TrayIconManager()
    {
        _contextMenu = new ContextMenuStrip();
        _openItem = _contextMenu.Items.Add("Open Caroline", null, (_, _) => OpenRequested?.Invoke(this, EventArgs.Empty));
        _contextMenu.Items.Add(new ToolStripSeparator());
        _contextMenu.Items.Add("Exit", null, (_, _) => ExitRequested?.Invoke(this, EventArgs.Empty));

        _notifyIcon = new NotifyIcon
        {
            Icon = LoadIcon(),
            Text = "Caroline",
            Visible = true,
            ContextMenuStrip = _contextMenu,
        };
        _notifyIcon.MouseClick += (_, e) =>
        {
            if (e.Button == MouseButtons.Left) OpenRequested?.Invoke(this, EventArgs.Empty);
        };
    }

    /// <summary>Shows (or updates the label of) an "Update to {version}" item right under "Open Caroline".
    /// Idempotent -- safe to call again with a newer version as later checks find one.</summary>
    public void ShowUpdateAvailable(string version)
    {
        if (_updateItem != null)
        {
            Logger.Log($"[TrayIconManager] ShowUpdateAvailable: item already present, updating label to '{version}'");
            _updateItem.Text = $"Update to {version}";
            return;
        }
        Logger.Log($"[TrayIconManager] ShowUpdateAvailable: adding tray item 'Update to {version}'");
        var index = _contextMenu.Items.IndexOf(_openItem) + 1;
        _updateItem = new ToolStripMenuItem($"Update to {version}", null, (_, _) =>
        {
            Logger.Log("[TrayIconManager] user clicked 'Update to ...' tray item");
            UpdateRequested?.Invoke(this, EventArgs.Empty);
        });
        _contextMenu.Items.Insert(index, _updateItem);
    }

    /// <summary>One-time balloon popup shown when a download actually starts -- per explicit
    /// instruction (2026-09-06): a 2+ minute download with literally nothing visible anywhere
    /// (confirmed live) reads as "did my click even do anything?". This fires once per download;
    /// the ongoing progress itself goes to every tab's status bar instead (see MainWindow's
    /// StatusChanged subscription) -- a balloon re-appearing on every percent tick would be far
    /// more annoying than the silence it replaces.</summary>
    public void ShowUpdateOngoingPopup()
    {
        Logger.Log("[TrayIconManager] ShowUpdateOngoingPopup");
        _notifyIcon.BalloonTipIcon = ToolTipIcon.Info;
        _notifyIcon.BalloonTipTitle = "Caroline";
        _notifyIcon.BalloonTipText = "Update ongoing... this may take a couple of minutes.";
        _notifyIcon.ShowBalloonTip(10_000);
    }

    /// <summary>Removes the "Update to ..." item the moment it's clicked -- per explicit
    /// instruction (2026-09-03): once an update is actually in flight (its own progress
    /// window now shows the user what's happening, see UpdateChecker.UpdateNowAsync), the
    /// tray item has nothing useful left to offer and could otherwise be double-clicked
    /// into launching a second installer instance on top of the first. If a future periodic
    /// check finds a still-newer version, ShowUpdateAvailable naturally re-adds it.</summary>
    public void HideUpdateAvailable()
    {
        if (_updateItem == null) return;
        Logger.Log("[TrayIconManager] HideUpdateAvailable: removing tray item");
        _contextMenu.Items.Remove(_updateItem);
        _updateItem = null;
    }

    private static Icon LoadIcon()
    {
        try
        {
            var uri = new Uri("pack://application:,,,/Resources/app.ico");
            var info = System.Windows.Application.GetResourceStream(uri);
            if (info?.Stream != null) return new Icon(info.Stream);
        }
        catch (Exception ex)
        {
            Logger.Log($"[TrayIconManager] LoadIcon: failed to load app.ico, falling back to system default: {ex.Message}");
        }
        return SystemIcons.Application;
    }

    public void Dispose()
    {
        _notifyIcon.Visible = false;
        _notifyIcon.Dispose();
    }
}

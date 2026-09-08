using System.Reflection;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

namespace CarolineInstaller.UI;

public partial class ProgressWindow : Window
{
    private readonly List<string> _bannerResourceNames;
    private readonly DispatcherTimer _bannerTimer;
    private int _bannerIndex;

    /// <summary>Raised when the user confirms they want to stop the install via the title bar's cancel button.</summary>
    public event Action? CancelRequested;

    public ProgressWindow()
    {
        InitializeComponent();

        StatusText.Text = "Checking for the latest version…";
        MinimizeButton.ToolTip = "Minimize";
        CancelButton.ToolTip = "Cancel";

        _bannerResourceNames = Assembly.GetExecutingAssembly().GetManifestResourceNames()
            .Where(n => n.Contains("Assets.Banners.", StringComparison.OrdinalIgnoreCase))
            .OrderBy(n => n)
            .ToList();

        _bannerTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(6) };
        _bannerTimer.Tick += (_, _) => CycleBanner();

        if (_bannerResourceNames.Count > 0)
        {
            CycleBanner();
            _bannerTimer.Start();
        }
    }

    private void CycleBanner()
    {
        if (_bannerResourceNames.Count == 0)
        {
            return;
        }
        var name = _bannerResourceNames[_bannerIndex % _bannerResourceNames.Count];
        _bannerIndex++;

        using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(name);
        if (stream is null)
        {
            return;
        }
        var bitmap = new BitmapImage();
        bitmap.BeginInit();
        bitmap.CacheOption = BitmapCacheOption.OnLoad;
        bitmap.StreamSource = stream;
        bitmap.EndInit();
        bitmap.Freeze();
        BannerImage.Source = bitmap;
    }

    public void SetVersionInfo(string version) => VersionText.Text = version;

    /// <summary>Updates the status line and switches the bar to indeterminate (for phases with no byte-level progress).</summary>
    public void SetStatus(string text)
    {
        Dispatcher.Invoke(() =>
        {
            StatusText.Text = text;
            ProgressBarControl.IsIndeterminate = true;
            SpeedEtaText.Text = "";
        });
    }

    /// <summary>Updates the status line with determinate download progress (percent/speed).</summary>
    public void SetDownloadProgress(string statusPrefix, DownloadProgress progress)
    {
        Dispatcher.BeginInvoke(() =>
        {
            StatusText.Text = statusPrefix;
            ProgressBarControl.IsIndeterminate = false;
            ProgressBarControl.Value = progress.FractionComplete;

            var mbPerSec = progress.BytesPerSecond / (1024.0 * 1024.0);
            SpeedEtaText.Text = $"{progress.FractionComplete:P0} — {mbPerSec:F1} MB/s";
        });
    }

    private void TitleBar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (e.ButtonState == MouseButtonState.Pressed)
        {
            DragMove();
        }
    }

    private void Minimize_Click(object sender, RoutedEventArgs e)
    {
        WindowState = WindowState.Minimized;
    }

    private void Cancel_Click(object sender, RoutedEventArgs e)
    {
        var result = MessageBox.Show(this,
            "Cancel setup?", "Caroline Setup", MessageBoxButton.YesNo, MessageBoxImage.Question);
        if (result == MessageBoxResult.Yes)
        {
            CancelRequested?.Invoke();
        }
    }
}

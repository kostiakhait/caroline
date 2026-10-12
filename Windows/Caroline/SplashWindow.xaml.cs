using System.IO;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Caroline.Services;

namespace Caroline;

/// <summary>
/// Fully transparent, chrome-less window shown while Caroline's own startup
/// connects to her backend -- cycles through CarolineInstaller's own
/// onboarding banners (rounded corners, see SplashWindow.xaml's ImageBrush)
/// with a "Connecting..." label underneath, same per-pixel-alpha
/// layered-window trick as before (WPF's AllowsTransparency), just with
/// content that actually reflects "still connecting" instead of a fixed
/// logo shown for a flat timer regardless of how long startup actually
/// takes. Dismissed by App.xaml.cs once the backend is confirmed listening
/// (or a fallback timeout), a left-click, or -- unchanged from before --
/// whichever comes first.
/// </summary>
public partial class SplashWindow : Window
{
    // Copied here at build time from CarolineInstaller/Assets/Banners (see
    // Caroline.csproj) -- reused rather than duplicating a separate image
    // set just for this.
    private static readonly string PhotosDir = Path.Combine(AppContext.BaseDirectory, "SplashBanners");
    private static readonly TimeSpan CycleInterval = TimeSpan.FromSeconds(2.5);

    private readonly DispatcherTimer _cycleTimer = new() { Interval = CycleInterval };
    private string[] _photos = Array.Empty<string>();
    private int _photoIndex;

    // Same fix as MainWindow.xaml.cs's ReinstallWindowIcon (2026-09-28): the taskbar/
    // Alt-Tab icon can show the generic Windows placeholder instead of app.ico, so the
    // icon is re-applied on a flat interval rather than relying on the XAML Icon=
    // attribute alone. Stopped on Closed since, unlike MainWindow, this window does not
    // live for the whole process.
    private static readonly TimeSpan IconReinstallInterval = TimeSpan.FromSeconds(10);
    private readonly DispatcherTimer _iconReinstallTimer = new() { Interval = IconReinstallInterval };

    public SplashWindow()
    {
        InitializeComponent();
        StartPhotoCycle();
        _cycleTimer.Tick += (_, _) => ShowNextPhoto();
        _iconReinstallTimer.Tick += (_, _) => ReinstallWindowIcon();
        _iconReinstallTimer.Start();
        Closed += (_, _) => _iconReinstallTimer.Stop();
    }

    private void ReinstallWindowIcon()
    {
        try
        {
            Icon = BitmapFrame.Create(new Uri("pack://application:,,,/Resources/app.ico"));
        }
        catch (Exception ex)
        {
            Logger.Log($"SplashWindow: ReinstallWindowIcon failed: {ex.Message}");
        }
    }

    private void StartPhotoCycle()
    {
        try
        {
            if (Directory.Exists(PhotosDir))
            {
                _photos = Directory.GetFiles(PhotosDir, "*.png");
            }
        }
        catch
        {
            _photos = Array.Empty<string>();
        }

        if (_photos.Length == 0)
        {
            // Fall back to the old static logo if the banners aren't there
            // for some reason (a dev tree layout that doesn't match the
            // published one, say) -- better a static image than a blank
            // splash.
            PhotoBrush.ImageSource = new BitmapImage(new Uri("pack://application:,,,/Resources/splash.png"));
            return;
        }

        // Random start so repeated launches don't always open on the same
        // banner first.
        _photoIndex = new Random().Next(_photos.Length);
        ShowPhoto(_photoIndex);
        if (_photos.Length > 1) _cycleTimer.Start();
    }

    private void ShowNextPhoto()
    {
        _photoIndex = (_photoIndex + 1) % _photos.Length;
        ShowPhoto(_photoIndex);
    }

    private void ShowPhoto(int index)
    {
        try
        {
            var bmp = new BitmapImage();
            bmp.BeginInit();
            bmp.CacheOption = BitmapCacheOption.OnLoad;
            bmp.UriSource = new Uri(_photos[index]);
            bmp.EndInit();
            PhotoBrush.ImageSource = bmp;
        }
        catch
        {
            // A single unreadable file shouldn't kill the whole cycle --
            // just leave whatever was showing before.
        }
    }

    private void OnMouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        _cycleTimer.Stop();
        Close();
    }
}

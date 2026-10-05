using System;
using System.IO;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Threading;

namespace Caroline;

/// <summary>
/// Fully transparent, chrome-less window shown while Caroline's own startup
/// connects to her backend -- cycles through CarolineInstaller's own
/// onboarding banners with a "Connecting..." label underneath, same
/// per-pixel-alpha layered-window trick as the WPF version (Avalonia's
/// TransparencyLevelHint), just with content that actually reflects "still
/// connecting" instead of a fixed logo shown for a flat timer regardless of
/// how long startup actually takes. Dismissed by App.axaml.cs once the
/// backend is confirmed listening (or a fallback timeout), or a left-click,
/// whichever comes first -- direct port of SplashWindow.xaml.cs, same
/// behavior, Avalonia's own Bitmap/DispatcherTimer/PointerPressed APIs in
/// place of WPF's BitmapImage/DispatcherTimer/MouseLeftButtonDown.
/// </summary>
public partial class SplashWindow : Window
{
    private static readonly string PhotosDir = Path.Combine(AppContext.BaseDirectory, "SplashBanners");
    private static readonly TimeSpan CycleInterval = TimeSpan.FromSeconds(2.5);

    private readonly DispatcherTimer _cycleTimer = new() { Interval = CycleInterval };
    private string[] _photos = Array.Empty<string>();
    private int _photoIndex;

    public SplashWindow()
    {
        InitializeComponent();
        StartPhotoCycle();
        _cycleTimer.Tick += (_, _) => ShowNextPhoto();
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
            // Fall back to the static logo (embedded AvaloniaResource, see
            // the csproj's AvaloniaResource include) if the banners aren't
            // there for some reason -- better a static image than a blank
            // splash.
            var fallback = new Bitmap(Avalonia.Platform.AssetLoader.Open(new Uri("avares://Caroline/Resources/splash.png")));
            PhotoBorder.Background = new ImageBrush(fallback) { Stretch = Stretch.UniformToFill };
            return;
        }

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
            var bitmap = new Bitmap(_photos[index]);
            PhotoBorder.Background = new ImageBrush(bitmap) { Stretch = Stretch.UniformToFill };
        }
        catch
        {
            // A single unreadable file shouldn't kill the whole cycle --
            // just leave whatever was showing before.
        }
    }

    private void OnPointerPressed(object? sender, PointerPressedEventArgs e)
    {
        if (!e.GetCurrentPoint(this).Properties.IsLeftButtonPressed) return;
        _cycleTimer.Stop();
        Close();
    }
}

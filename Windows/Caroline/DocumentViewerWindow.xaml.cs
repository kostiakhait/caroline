using System.IO;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Media.Imaging;
using Caroline.Services;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

namespace Caroline;

public enum ViewerOutcome { Saved, Cancelled, Closed, Error }

/// <summary>Config handed back by the backend's document:openForEdit call (see officeEditor.ts) -- everything
/// OnlyOffice's own JS API needs to render/edit the document, passed through to office_editor.html via its
/// query string (see ShowOffice()).</summary>
public sealed record OfficeEditorConfig(
    string DocumentType, string FileType, bool Editable, string Key,
    string DocumentUrl, string OnlyofficeUrl, string Title, string? CallbackUrl);

/// <summary>
/// Caroline's own floating window for showing a photo/video or editing a
/// document, opened by the "open_in_viewer" backend tool (see
/// MainWindow.xaml.cs's WebMessageReceived handler, which creates this).
///
/// Documents are edited via a real OnlyOffice Document Server -- the same
/// one Notes already uses in production (see backend/src/officeEditor.ts's
/// doc comment) -- rendered here through its own WebView2 control loading
/// a small local page (office_editor.html) that instantiates OnlyOffice's
/// DocsAPI.DocEditor. This replaced an earlier approach that launched a
/// real soffice.exe process and reparented its window into this one by
/// hand (SetParent + stripped title bar) -- confirmed live that approach
/// was fragile (it briefly grabbed LibreOffice's own startup splash screen
/// instead of the real document window) and heavier than necessary now
/// that a proper embeddable editor is available.
/// </summary>
public partial class DocumentViewerWindow : Window
{
    private readonly string _path = "";
    private readonly string _kind; // "image" | "video" | "office" | "login"
    private readonly Action<ViewerOutcome, string?, string?>? _onDone;
    private readonly Action<string?, string?, bool, bool, bool>? _onLoginDone;
    private bool _resultReported;
    private WebView2? _officeWebView;
    private string? _pagePayloadJson;
    // Set by OnOfficeWebMessage the moment OnlyOffice's own iframe reports it
    // could not open the document -- see office_editor.html's postToHost doc
    // comment. Report() checks this so OnClosing/OnOfficeDoneClicked can no
    // longer claim "saved" for a document that was never actually opened.
    private string? _officeErrorMessage;

    /// <summary>
    /// Web-page viewers ("code" = Monaco, "slideshow"): the page is loaded from
    /// wwwroot and receives the backend's open_editor event (payloadJson) once it
    /// has finished loading. path is the file a code view saves back to, or empty.
    /// </summary>
    public DocumentViewerWindow(string title, string kind, string path, string payloadJson, Action<ViewerOutcome, string?, string?> onDone)
    {
        InitializeComponent();
        _path = path;
        _kind = kind;
        _onDone = onDone;
        _pagePayloadJson = payloadJson;
        TitleText.Text = title.Length > 0 ? title : (path.Length > 0 ? Path.GetFileName(path) : kind);
        _ = ShowWebPage(kind == "code" ? "monaco_viewer.html" : "slideshow.html");
    }

    public DocumentViewerWindow(string path, string kind, Action<ViewerOutcome, string?, string?> onDone)
    {
        InitializeComponent();
        _path = path;
        _kind = kind;
        _onDone = onDone;
        TitleText.Text = Path.GetFileName(path);

        switch (kind)
        {
            case "image": ShowImage(); break;
            case "video": ShowVideo(); break;
            case "payment":
                TitleText.Text = "Top up SquirrelWisdom balance";
                _ = ShowPayment(path);
                break;
        }
    }

    /// <summary>
    /// Part 5's "payment" viewer kind -- navigates a plain WebView2 straight
    /// at the checkout_url reforce's Revolut.create_topup already returns
    /// (see subscriptionMode.ts's createTopupCheckoutUrl); no custom checkout
    /// page of our own to build/host, and no result to report back other
    /// than "the user is done browsing this" (Revolut's own webhook confirms
    /// completion server-side, not this window closing).
    /// </summary>
    private async Task ShowPayment(string checkoutUrl)
    {
        ViewButtons.Visibility = Visibility.Visible;
        var webView = new WebView2();
        OfficeHost.Children.Add(webView);
        OfficeHost.Visibility = Visibility.Visible;
        _officeWebView = webView;
        try
        {
            var dataDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "Caroline", "webview2-payment");
            var env = await CoreWebView2Environment.CreateAsync(userDataFolder: dataDir);
            await webView.EnsureCoreWebView2Async(env);
            webView.Source = new Uri(checkoutUrl);
        }
        catch (Exception ex)
        {
            OfficeHost.Visibility = Visibility.Collapsed;
            ShowError($"Could not open the payment page: {ex.Message}");
        }
    }

    /// <summary>Documents: opened via the OnlyOffice flow (see class doc comment above).</summary>
    public DocumentViewerWindow(string path, OfficeEditorConfig config, Action<ViewerOutcome, string?, string?> onDone)
    {
        InitializeComponent();
        _path = path;
        _kind = "office";
        _onDone = onDone;
        TitleText.Text = Path.GetFileName(path);
        _ = ShowOffice(config);
    }

    /// <summary>
    /// The SquirrelWisdom login form (see login.ts's ensure_squirrelwisdom_login
    /// tool and MainWindow.xaml.cs's OnOpenLogin) -- reuses this same floating
    /// window shell rather than a separate one, since it's exactly the same
    /// "one small task in its own window" shape as image/video/document.
    /// onLoginDone is called with (email, password, cancelled=false, isRegister) on
    /// submit, (null, null, cancelled=true, false) if the user cancels/closes
    /// without logging in, or (null, null, cancelled=true, false, openSettingsInstead=
    /// true) if they click "I already have a Claude account" (see noAiAtAll below).
    /// Credentials never touch this app's WebView2/chat page or the model -- they go
    /// straight from here to MainWindow's caller.
    ///
    /// noAiAtAll: per explicit instruction (2026-09-07) -- true only for the one case
    /// where Caroline genuinely cannot talk at all (no Claude account AND no
    /// SquirrelWisdom login), as opposed to a single SW-gated feature being
    /// unavailable while chat itself works fine (see login.ts's openLoginRequest).
    /// Swaps in honest copy for that case and shows the "I have Claude" shortcut,
    /// which would be redundant/confusing to show when Claude obviously already
    /// works (that's the only way this window opens for anything else).
    /// </summary>
    public DocumentViewerWindow(string? loginError, bool noAiAtAll, Action<string?, string?, bool, bool, bool> onLoginDone)
    {
        InitializeComponent();
        _kind = "login";
        _onLoginDone = onLoginDone;
        TitleText.Text = "Log in to SquirrelWisdom";
        ShowLogin(loginError, noAiAtAll);
    }

    private void ShowLogin(string? error, bool noAiAtAll)
    {
        LoginButtons.Visibility = Visibility.Visible;
        LoginPanel.Visibility = Visibility.Visible;
        if (noAiAtAll)
        {
            LoginSubtitleText.Text = "Caroline can't talk to you right now -- log in or register with SquirrelWisdom " +
                "below, or use your own Claude account instead.";
            LoginOpenSettingsButton.Visibility = Visibility.Visible;
        }
        if (!string.IsNullOrEmpty(error))
        {
            LoginErrorText.Text = error;
            LoginErrorText.Visibility = Visibility.Visible;
        }
    }

    private void OnLoginClicked(object sender, RoutedEventArgs e)
    {
        var email = LoginEmailBox.Text.Trim();
        var password = LoginPasswordBox.Password;
        if (email.Length == 0 || password.Length == 0)
        {
            LoginErrorText.Text = "Enter both email and password.";
            LoginErrorText.Visibility = Visibility.Visible;
            return;
        }
        _resultReported = true;
        _onLoginDone!(email, password, false, RegisterToggle.IsChecked == true, false);
        Close();
    }

    private void OnLoginCancelClicked(object sender, RoutedEventArgs e)
    {
        _resultReported = true;
        _onLoginDone!(null, null, true, false, false);
        Close();
    }

    private void OnLoginOpenSettingsClicked(object sender, RoutedEventArgs e)
    {
        _resultReported = true;
        _onLoginDone!(null, null, true, false, true);
        Close();
    }

    private void OnRegisterToggleChanged(object sender, RoutedEventArgs e)
    {
        var isRegister = RegisterToggle.IsChecked == true;
        LoginTitleText.Text = isRegister ? "Register with SquirrelWisdom" : "Log in to SquirrelWisdom";
        LoginButton.Content = isRegister ? "Register" : "Log In";
    }

    private void ShowImage()
    {
        ViewButtons.Visibility = Visibility.Visible;
        try
        {
            var bmp = new BitmapImage();
            bmp.BeginInit();
            bmp.CacheOption = BitmapCacheOption.OnLoad;
            bmp.UriSource = new Uri(_path);
            bmp.EndInit();
            ImageView.Source = bmp;
            ImageView.Visibility = Visibility.Visible;
        }
        catch (Exception ex)
        {
            ShowError($"Could not load image: {ex.Message}");
        }
    }

    private void ShowVideo()
    {
        ViewButtons.Visibility = Visibility.Visible;
        try
        {
            VideoView.Source = new Uri(_path);
            VideoView.Visibility = Visibility.Visible;
            VideoView.Play();
        }
        catch (Exception ex)
        {
            ShowError($"Could not load video: {ex.Message}");
        }
    }

    private async Task ShowWebPage(string page)
    {
        ViewButtons.Visibility = Visibility.Visible;
        OfficeHost.Visibility = Visibility.Visible;
        StatusText.Text = "Loading…";
        StatusText.Visibility = Visibility.Visible;

        var webView = new WebView2();
        OfficeHost.Children.Add(webView);
        _officeWebView = webView;
        try
        {
            var dataDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "Caroline", "webview2-viewer");
            var env = await CoreWebView2Environment.CreateAsync(userDataFolder: dataDir);
            await webView.EnsureCoreWebView2Async(env);

            webView.CoreWebView2.SetVirtualHostNameToFolderMapping(
                "caroline.local", Path.Combine(AppContext.BaseDirectory, "wwwroot"), CoreWebView2HostResourceAccessKind.Allow);
            webView.CoreWebView2.WebMessageReceived += OnWebPageMessage;
            webView.CoreWebView2.NavigationCompleted += (_, args) =>
            {
                if (!args.IsSuccess)
                {
                    ShowError($"Could not load the viewer (error {args.WebErrorStatus}).");
                    return;
                }
                StatusText.Visibility = Visibility.Collapsed;
                if (_pagePayloadJson != null) webView.CoreWebView2.PostWebMessageAsJson(_pagePayloadJson);
            };
            webView.Source = new Uri($"https://caroline.local/{page}");
        }
        catch (Exception ex)
        {
            OfficeHost.Visibility = Visibility.Collapsed;
            ShowError($"Could not start the viewer: {ex.Message}");
        }
    }

    /// <summary>Code view save: the page sends the full text, which overwrites the
    /// file after keeping the previous version as .bak. Only code opened from a
    /// real file path is ever saveable (see viewer_plugin's show_code).</summary>
    private void OnWebPageMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        using var doc = JsonDocument.Parse(e.WebMessageAsJson);
        var root = doc.RootElement;
        if (!root.TryGetProperty("type", out var typeEl) || typeEl.GetString() != "save") return;
        if (string.IsNullOrEmpty(_path))
        {
            ShowError("This code has no file to save to.");
            return;
        }
        var content = root.GetProperty("content").GetString() ?? "";
        try
        {
            if (File.Exists(_path)) File.Copy(_path, _path + ".bak", overwrite: true);
            File.WriteAllText(_path, content, new UTF8Encoding(false));
        }
        catch (Exception ex)
        {
            ShowError($"Could not save: {ex.Message}");
        }
    }

    private void ShowError(string message)
    {
        StatusText.Text = message;
        StatusText.Visibility = Visibility.Visible;
        if (OfficeButtons.Visibility != Visibility.Visible) ViewButtons.Visibility = Visibility.Visible;
    }

    private async Task ShowOffice(OfficeEditorConfig config)
    {
        OfficeButtons.Visibility = Visibility.Visible;
        OfficeHost.Visibility = Visibility.Visible;
        StatusText.Text = "Opening document…";
        StatusText.Visibility = Visibility.Visible;

        var webView = new WebView2();
        OfficeHost.Children.Add(webView);
        _officeWebView = webView;

        try
        {
            // Separate profile directory from the main chat WebView2 -- each
            // WebView2 instance in a process needs to either share the exact
            // same user data folder+environment or use a distinct one; a
            // fresh dedicated one here is simplest and avoids any chance of
            // interfering with the chat page's own session/cache.
            var dataDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "Caroline", "webview2-office");
            var env = await CoreWebView2Environment.CreateAsync(userDataFolder: dataDir);
            await webView.EnsureCoreWebView2Async(env);

            webView.CoreWebView2.NavigationCompleted += (_, args) =>
            {
                StatusText.Visibility = Visibility.Collapsed;
                if (!args.IsSuccess) ShowError($"Could not load document editor (error {args.WebErrorStatus}).");
            };
            webView.CoreWebView2.WebMessageReceived += OnOfficeWebMessage;

            // Virtual host mapping, not file:///... -- same reasoning as
            // MainWindow's chat WebView2 (see its own comment): avoids
            // Chromium's per-file: unique-origin restriction for any local
            // relative resource this page might ever load.
            var wwwrootDir = Path.Combine(AppContext.BaseDirectory, "wwwroot");
            webView.CoreWebView2.SetVirtualHostNameToFolderMapping(
                "caroline.local", wwwrootDir, CoreWebView2HostResourceAccessKind.Allow);
            var query = string.Join("&",
                $"documentType={System.Net.WebUtility.UrlEncode(config.DocumentType)}",
                $"fileType={System.Net.WebUtility.UrlEncode(config.FileType)}",
                $"editable={(config.Editable ? "true" : "false")}",
                $"key={System.Net.WebUtility.UrlEncode(config.Key)}",
                $"title={System.Net.WebUtility.UrlEncode(config.Title)}",
                $"documentUrl={System.Net.WebUtility.UrlEncode(config.DocumentUrl)}",
                $"onlyofficeUrl={System.Net.WebUtility.UrlEncode(config.OnlyofficeUrl)}",
                config.CallbackUrl != null ? $"callbackUrl={System.Net.WebUtility.UrlEncode(config.CallbackUrl)}" : "");
            webView.Source = new Uri($"https://caroline.local/office_editor.html?{query}");
        }
        catch (Exception ex)
        {
            OfficeHost.Visibility = Visibility.Collapsed;
            ShowError($"Could not start the document editor: {ex.Message}");
        }
    }

    /// <summary>office_editor.html's postToHost -- currently only "office_error"
    /// (OnlyOffice itself failed to open/load the document; see that page's own
    /// doc comment for the 2026-10-07 incident this fixes).</summary>
    private void OnOfficeWebMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        try
        {
            using var doc = JsonDocument.Parse(e.WebMessageAsJson);
            var root = doc.RootElement;
            if (!root.TryGetProperty("type", out var typeEl) || typeEl.GetString() != "office_error") return;
            _officeErrorMessage = root.TryGetProperty("message", out var msgEl) ? msgEl.GetString() : "OnlyOffice could not open this document.";
            ShowError(_officeErrorMessage!);
        }
        catch (Exception ex)
        {
            Logger.Log($"DocumentViewerWindow: OnOfficeWebMessage failed to parse: {ex}");
        }
    }

    private void OnOfficeDoneClicked(object sender, RoutedEventArgs e)
    {
        Report(ViewerOutcome.Saved);
        Close();
    }

    private void OnCloseViewClicked(object sender, RoutedEventArgs e)
    {
        Report(ViewerOutcome.Closed);
        Close();
    }

    private void OnClosing(object? sender, System.ComponentModel.CancelEventArgs e)
    {
        try { VideoView.Stop(); } catch { /* not playing */ }
        if (_resultReported) return;
        if (_kind == "login")
        {
            // Closed via the X button without submitting -- same as Cancel.
            _resultReported = true;
            _onLoginDone!(null, null, true, false, false);
            return;
        }
        // Closed via the window's own X button rather than a toolbar button --
        // an office session always syncs back whatever OnlyOffice already
        // saved server-side (see server.ts's "editor_result" handler), so
        // this is "saved" too, same as clicking Done -- UNLESS OnlyOffice
        // itself already reported it never actually opened the document
        // (_officeErrorMessage, set by OnOfficeWebMessage), in which case
        // Report() below overrides this to "error" rather than lying that
        // nonexistent edits were saved.
        Report(_kind == "office" ? ViewerOutcome.Saved : ViewerOutcome.Closed);
    }

    private void Report(ViewerOutcome outcome)
    {
        if (_resultReported) return;
        _resultReported = true;
        if (_officeErrorMessage != null) outcome = ViewerOutcome.Error;
        _onDone!(outcome, _path, _officeErrorMessage);
    }
}

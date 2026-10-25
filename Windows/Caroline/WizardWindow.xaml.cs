using System.Diagnostics;
using System.IO;
using System.Windows;
using Caroline.Services;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

namespace Caroline;

/// <summary>
/// The start-up wizard (backend-py/app/wizard.py, page backend-py/app/assets/wizard.html): an e-mail and password to
/// sign in or register, the user's own Claude/OpenAI subscriptions, and the mailboxes and messengers of Caroline and
/// her owner. The page is served by the backend itself (http://127.0.0.1:&lt;port&gt;/wizard), so it and its API share one
/// origin; this window is only a white, always-on-top frame around it.
///
/// The page tells this window it is finished by changing its document title ("caroline-wizard-done" when the user
/// completed it, "caroline-wizard-dismissed" for "Finish later"); the window closes itself on either. A link the page
/// opens elsewhere (an OpenAI sign-in page) goes to the system browser, never into this window.
/// Same shell as caroline_shell.py's open_wizard on Linux.
/// </summary>
public partial class WizardWindow : Window
{
    private const string DoneTitle = "caroline-wizard-done";
    private const string DismissedTitle = "caroline-wizard-dismissed";

    private readonly string _origin;

    public WizardWindow(int backendPort)
    {
        InitializeComponent();
        _origin = $"http://127.0.0.1:{backendPort}/";
        Loaded += async (_, _) => await InitAsync();
    }

    private async Task InitAsync()
    {
        try
        {
            // Its own profile folder, like the payment window's: the wizard shares nothing with the chat's WebView2.
            var dataDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "Caroline", "webview2-wizard");
            var browser = new WebView2();
            Host.Children.Add(browser);
            var env = await CoreWebView2Environment.CreateAsync(userDataFolder: dataDir);
            await browser.EnsureCoreWebView2Async(env);
            browser.DefaultBackgroundColor = System.Drawing.Color.White;
            var core = browser.CoreWebView2;
            core.DocumentTitleChanged += (_, _) =>
            {
                if (core.DocumentTitle == DoneTitle || core.DocumentTitle == DismissedTitle) Close();
            };
            core.NewWindowRequested += (_, e) =>
            {
                e.Handled = true;
                OpenExternal(e.Uri);
            };
            core.NavigationStarting += (_, e) =>
            {
                if (!e.Uri.StartsWith(_origin, StringComparison.Ordinal))
                {
                    e.Cancel = true;
                    OpenExternal(e.Uri);
                }
            };
            browser.Source = new Uri(_origin + "wizard");
        }
        catch (Exception ex)
        {
            Logger.Log($"WizardWindow: could not open the wizard page: {ex.Message}");
            Close();
        }
    }

    private static void OpenExternal(string uri)
    {
        // Only web links: a page must not be able to start anything else through this.
        if (!Uri.TryCreate(uri, UriKind.Absolute, out var parsed) || (parsed.Scheme != Uri.UriSchemeHttps && parsed.Scheme != Uri.UriSchemeHttp))
            return;
        try
        {
            Process.Start(new ProcessStartInfo(parsed.AbsoluteUri) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            Logger.Log($"WizardWindow: could not open {parsed.Host}: {ex.Message}");
        }
    }
}

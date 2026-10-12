namespace Caroline.Services;

/// <summary>
/// Every WebMessageReceived handler in this app acted on whatever JSON showed
/// up with no check on who sent it. In practice every WebView2 instance here
/// only ever navigates to this app's own content (the "caroline.local"
/// virtual host, or a local file:// page for VisualModeWindow), so this has
/// never been reachable from an actual untrusted page -- but that is a
/// property of today's navigation policy, not something the message handler
/// itself enforces, and CoreWebView2WebMessageReceivedEventArgs.Source is
/// free to check. Treats any message from the trusted virtual host OR a
/// local file:// page as Caroline's own.
/// </summary>
internal static class WebViewOriginGuard
{
    public const string TrustedVirtualHost = "https://caroline.local";

    public static bool IsTrustedSource(string? source)
    {
        if (string.IsNullOrEmpty(source))
        {
            return false;
        }
        return source.StartsWith(TrustedVirtualHost, System.StringComparison.OrdinalIgnoreCase)
            || source.StartsWith("file://", System.StringComparison.OrdinalIgnoreCase);
    }
}

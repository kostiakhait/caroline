"""Shared "is this a cross-origin browser request" check for Caroline's
loopback-only local HTTP APIs (main.py's local port, supervisor.py).

Being bound to 127.0.0.1 only blocks a remote network attacker, but NOT a
malicious web page: browsers happily send cross-origin requests to
localhost (no CORS preflight for a plain POST with no custom headers --
the page just can't read the response, which is irrelevant for an action
like /stop or /api/control that doesn't need to). A legitimate caller
(curl, httpx, Caroline's own WebView2 frontend) either sends no Origin
header at all, or one that already matches this same loopback origin;
only a real cross-origin browser request sends some OTHER Origin.

Deliberately stdlib-only -- supervisor.py's own docstring requires it to
stay outside the `app` package's heavy import graph.
"""

from __future__ import annotations


# The WPF shell's WebView2 frontend never actually runs at http://127.0.0.1:
# <port> -- every window maps its wwwroot to this fixed virtual host via
# CoreWebView2.SetVirtualHostNameToFolderMapping("caroline.local", ...)
# (MainWindow.xaml.cs, DocumentViewerWindow.xaml.cs), specifically to get a
# real, stable origin for Chromium's relative-resource loading (see that
# call site's own comment). So chat.js's own fetch() calls to the local
# backend are themselves cross-origin by Chromium's reckoning and DO carry
# this exact Origin header -- it has to be allow-listed, not just 127.0.0.1/
# localhost, or the real frontend breaks along with any actual attacker.
CAROLINE_FRONTEND_ORIGIN = "https://caroline.local"


def is_cross_origin_browser_request(origin_header: str | None, port: int) -> bool:
    if not origin_header:
        return False
    allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", CAROLINE_FRONTEND_ORIGIN}
    return origin_header not in allowed


_LOOPBACK_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}


def is_foreign_host(host_header: str | None) -> bool:
    """True when a request to a loopback listener names some other host. The
    Origin check above does not cover a page whose own domain was re-pointed at
    127.0.0.1 (DNS rebinding): its requests are same-origin to it, so a GET
    carries no Origin at all, and it can read the response. Its Host header
    still names its own domain. Every real caller here addresses 127.0.0.1 or
    localhost; no Host header at all (HTTP/1.0) is let through."""
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:port
        host = host[1:host.find("]")] if "]" in host else host
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host not in _LOOPBACK_HOSTNAMES

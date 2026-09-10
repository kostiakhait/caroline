"""app-browser -- ports backend/src/appBrowser.ts. Caroline's own embedded
multi-window browser -- one persistent, labeled WebView2 window per site
(e.g. "whatsapp", "telegram", "facebook", "slack"), living inside the app
instead of a separate standalone Chromium process. Window lifecycle
(open/navigate/close/list/screenshot/scroll/is_visible_on_top/
fill_file_dialog) and real OS-level input go through AppBrowserHost.cs's
HTTP bridge (http://127.0.0.1:8767, runs INSIDE the WPF process, untouched
by this rewrite); page-content operations (snapshot/find/click/type/
press_key/evaluate) go over a real CDP connection instead
(app_browser_cdp.py), since CDP isn't subject to a page's own CSP the way
ExecuteScriptAsync-injected script is.

This is Caroline's PRIMARY browsing tool -- prefer it over the standalone
caroline-browser/other browser MCP servers for ordinary web/app tasks.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.plugins import app_browser_cdp as cdp
from app.plugins.loader import Plugin, PluginTool
from app.plugins.voice_api import describe_image_cheap
from app.policies import close_windows_after_task_instruction, prefer_cropped_screenshots_instruction, prefer_window_targeted_input_instruction

APP_BROWSER_HOST = "http://127.0.0.1:8767"

# /open no longer blocks on a full page load, but a cold WebView2
# environment for a brand-new profile can still take a while to spin up
# its own runtime process, especially under system load.
DEFAULT_TIMEOUT_S = 45.0
OPEN_TIMEOUT_S = 90.0

_ACCOUNT_HINT = (
    "Embedded browser call failed: {exc}. Is Caroline's WPF app running (this tool only works "
    "inside the desktop app, not headless)? If the problem persists, the standalone caroline-browser "
    "tools remain available as a fallback."
)


async def _call(path: str, body: dict[str, Any] | None, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            res = await client.post(f"{APP_BROWSER_HOST}{path}", json=body or {})
            try:
                return res.json()
            except Exception:
                return {}
    except Exception as exc:
        raise RuntimeError(_ACCOUNT_HINT.format(exc=exc)) from exc


async def _get(path: str, timeout_s: float = 10.0) -> Any:
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            res = await client.get(f"{APP_BROWSER_HOST}{path}")
            try:
                return res.json()
            except Exception:
                return []
    except Exception as exc:
        raise RuntimeError(_ACCOUNT_HINT.format(exc=exc)) from exc


async def _get_cdp_port(label: str) -> int:
    """Every CDP-routed tool needs the label's window to exist first --
    replicates AppBrowserHost's old lazy-open-if-needed behavior now that
    these ops bypass that HTTP bridge entirely."""
    got = await _call("/get_port", {"label": label})
    if "cdpPort" in got:
        return got["cdpPort"]
    opened = await _call("/open", {"label": label}, OPEN_TIMEOUT_S)
    return opened["cdpPort"]


async def open_app_browser(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"label": args["label"]}
    if args.get("url"):
        body["url"] = args["url"]
    result = await _call("/open", body, OPEN_TIMEOUT_S)
    return {"text": json.dumps(result, ensure_ascii=False)}


async def app_browser_navigate(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    result = await _call("/navigate", {"label": args["label"], "url": args["url"]})
    return {"text": json.dumps(result, ensure_ascii=False)}


async def app_browser_snapshot(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    port = await _get_cdp_port(args["label"])
    tagged = await cdp.snapshot(port)
    return {"text": json.dumps(tagged, ensure_ascii=False)}


async def app_browser_find(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    port = await _get_cdp_port(args["label"])
    matches = await cdp.find(port, args["text"])
    return {"text": json.dumps(matches, ensure_ascii=False)}


async def app_browser_click(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    label = args["label"]
    if args.get("x") is not None and args.get("y") is not None:
        result = await _call("/click", {"label": label, "x": args["x"], "y": args["y"]})
        return {"text": json.dumps(result, ensure_ascii=False)}
    if args.get("real"):
        result = await _call("/click", {"label": label, "ref": args.get("ref"), "selector": args.get("selector"), "real": True})
        return {"text": json.dumps(result, ensure_ascii=False)}
    port = await _get_cdp_port(label)
    result = await cdp.click(port, args.get("ref"), args.get("selector"))
    return {"text": json.dumps({"result": result}, ensure_ascii=False)}


async def app_browser_scroll(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    body = {
        "label": args["label"], "ref": args.get("ref"), "selector": args.get("selector"),
        "x": args.get("x"), "y": args.get("y"), "clicks": args.get("clicks"),
    }
    result = await _call("/scroll", body)
    return {"text": json.dumps(result, ensure_ascii=False)}


async def app_browser_type(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    label = args["label"]
    if args.get("real"):
        result = await _call("/type", {"label": label, "ref": args.get("ref"), "selector": args.get("selector"), "text": args["text"], "real": True})
        return {"text": json.dumps(result, ensure_ascii=False)}
    port = await _get_cdp_port(label)
    result = await cdp.type_text(port, args["text"], args.get("ref"), args.get("selector"))
    return {"text": json.dumps({"result": result}, ensure_ascii=False)}


async def app_browser_press_key(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    label = args["label"]
    if args.get("real"):
        result = await _call("/press_key", {"label": label, "key": args["key"], "real": True})
        return {"text": json.dumps(result, ensure_ascii=False)}
    port = await _get_cdp_port(label)
    result = await cdp.press_key(port, args["key"])
    return {"text": result}


async def app_browser_screenshot(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    body = {
        "label": args["label"], "x": args.get("x"), "y": args.get("y"),
        "width": args.get("width"), "height": args.get("height"), "maxWidth": args.get("maxWidth"),
    }
    result = await _call("/screenshot", body)
    image_b64 = result.get("imageBase64")
    if not image_b64:
        raise RuntimeError("No image returned")
    return {"text": f"Screenshot of {args['label']}", "image_base64": image_b64, "mime_type": "image/png"}


async def app_browser_describe(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    result = await _call("/screenshot", {"label": args["label"]})
    image_b64 = result.get("imageBase64")
    if not image_b64:
        raise RuntimeError("No image returned")
    payload = await describe_image_cheap(image_b64)
    return {"text": json.dumps(payload, ensure_ascii=False)}


async def app_browser_evaluate(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    port = await _get_cdp_port(args["label"])
    result = await cdp.evaluate(port, args["fn"])
    return {"text": json.dumps(result, ensure_ascii=False)}


async def app_browser_is_visible_on_top(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    result = await _call("/is_visible_on_top", {"label": args["label"]})
    return {"text": json.dumps(result, ensure_ascii=False)}


async def app_browser_fill_file_dialog(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    body = {"paths": args["paths"], "timeoutMs": args.get("timeoutMs")}
    result = await _call("/fill_file_dialog", body)
    return {"text": json.dumps(result, ensure_ascii=False)}


async def close_app_browser(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    result = await _call("/close", {"label": args["label"]})
    return {"text": json.dumps(result, ensure_ascii=False)}


async def list_app_browsers(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    result = await _get("/list")
    return {"text": json.dumps(result, ensure_ascii=False)}


def _embedded_browser_instruction() -> str:
    return (
        "For web/app browsing (WhatsApp Web, Telegram Web, Facebook, Slack, or general sites), use ONLY the "
        "app_browser_* tools (open_app_browser, app_browser_navigate/snapshot/find/click/type/press_key/"
        'screenshot/evaluate, close_app_browser, list_app_browsers) -- each is a persistent window living '
        'inside Caroline\'s own app (pick a short label like "whatsapp" or "telegram" per site; reuse the same '
        "label to keep working in the same window with the same login/cookies, a new label opens a separate "
        "one). This is a hard default, not a preference: do NOT use the standalone caroline-browser tools (or "
        "any other browser MCP server) without asking the user first and getting their explicit go-ahead for "
        "that specific case -- even when the embedded one seems to be struggling with something. Explain what "
        "you're hitting and why you think the standalone browser is needed, then wait for them to actually say "
        "yes before switching. Never make that call yourself. If the embedded tools don't seem to be working, "
        "see the embedded-browser-troubleshooting skill before considering the standalone one at all."
    )


def _cheap_image_description_instruction() -> str:
    return (
        "When you need to understand a screenshot but don't need exact pixel/element coordinates (checking "
        "whether something finished, reading an error message, confirming what page you're on), prefer "
        "app_browser_describe over app_browser_screenshot -- it answers in text instead of putting the image "
        "itself into your own context, which costs real tokens on every call. Only reach for the real "
        "screenshot/vision path when you genuinely need to see pixels yourself (to click by x,y) or the cheap "
        "description turns out not to be enough."
    )


def _table_size_guidance_instruction() -> str:
    return (
        "For a small table (roughly up to ~10 rows), use standard markdown pipe-table syntax directly in your "
        "reply -- it renders as a real table in the chat, not raw text. For a genuinely large tabular data dump "
        "(many rows and/or columns -- a contact list export, a big data pull), don't paste it into the chat at "
        "all: use the embedded browser to open/create a spreadsheet (e.g. Google Sheets) and put the data there "
        "instead, then tell the user briefly what you did and point them to it."
    )


def _usage_instructions() -> str:
    return "\n\n".join((
        _embedded_browser_instruction(),
        prefer_window_targeted_input_instruction(),
        _cheap_image_description_instruction(),
        prefer_cropped_screenshots_instruction(),
        _table_size_guidance_instruction(),
        close_windows_after_task_instruction(),
    ))


PLUGIN = Plugin(
    name="appbrowser",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "open_app_browser",
            "Open (or focus, if already open) Caroline's own embedded browser window for a given label, "
            "optionally navigating it to a URL. This is her PRIMARY browser -- prefer it over the standalone "
            "caroline-browser tools for ordinary web/app tasks (WhatsApp Web, Telegram Web, Facebook, Slack, "
            "general browsing). Each label is its own persistent window living inside the app, not a "
            "separate Chrome process.",
            {"label": str, "url": str | None}, open_app_browser,
        ),
        PluginTool(
            "app_browser_navigate",
            "Navigate an already-open embedded browser window (see open_app_browser) to a new URL.",
            {"label": str, "url": str}, app_browser_navigate,
        ),
        PluginTool(
            "app_browser_snapshot",
            "Tag every visible interactive element in the labeled window with a stable ref (e1, e2, ...) and "
            "return {ref, role, name}. Refs are used by app_browser_click/app_browser_type and stay valid "
            "until the next snapshot/find or a navigation. Goes over a real CDP connection (not affected by "
            "the page's own CSP).",
            {"label": str}, app_browser_snapshot,
        ),
        PluginTool(
            "app_browser_find",
            "Like app_browser_snapshot, but filtered to elements whose accessible name contains the given "
            "text (case-insensitive).",
            {"label": str, "text": str}, app_browser_find,
        ),
        PluginTool(
            "app_browser_click",
            "Click an element in the labeled window by ref (from app_browser_snapshot/app_browser_find), raw "
            "CSS selector, or raw viewport pixel coordinates (x,y -- the same coordinate space as "
            "app_browser_screenshot's image). x,y always does a real OS-level click. For ref/selector: if a "
            "normal click doesn't register, retry with real:true for an actual OS-level click "
            "(indistinguishable from a human click, but moves the real cursor -- prefer the default first).",
            {
                "label": str, "ref": str | None, "selector": str | None,
                "x": int | None, "y": int | None, "real": bool | None,
            }, app_browser_click,
        ),
        PluginTool(
            "app_browser_scroll",
            "Scrolls the labeled window with a real OS-level mouse wheel, aimed at a specific point -- either "
            "an element (ref/selector) or raw viewport coordinates (x,y). clicks is signed like a physical "
            "wheel notch: negative scrolls down (toward newer content), positive scrolls up. Default -3.",
            {
                "label": str, "ref": str | None, "selector": str | None,
                "x": int | None, "y": int | None, "clicks": int | None,
            }, app_browser_scroll,
        ),
        PluginTool(
            "app_browser_type",
            "Type text into an element in the labeled window by ref or CSS selector, over a real CDP "
            "connection. Same real:true escalation as app_browser_click, for sites that reject even "
            "CDP-driven input on protected fields.",
            {"label": str, "ref": str | None, "selector": str | None, "text": str, "real": bool | None}, app_browser_type,
        ),
        PluginTool(
            "app_browser_press_key",
            'Press a key on the currently-focused element in the labeled window, e.g. "Enter", or '
            '"Control+A" for a combo -- over a real CDP connection by default (triggers the browser\'s '
            "native default action, e.g. submits a form on Enter). Pass real:true for an actual OS-level "
            "keystroke instead.",
            {"label": str, "key": str, "real": bool | None}, app_browser_press_key,
        ),
        PluginTool(
            "app_browser_screenshot",
            "Capture the labeled window's current page as a PNG (already zoomed out and capped to a "
            "reasonable width by default). Optional x/y/width/height crop a sub-rectangle; maxWidth "
            "overrides the default cap. Prefer a crop over the full page whenever you already know roughly "
            "where the thing you need is.",
            {
                "label": str, "x": int | None, "y": int | None,
                "width": int | None, "height": int | None, "maxWidth": int | None,
            }, app_browser_screenshot,
        ),
        PluginTool(
            "app_browser_describe",
            "Cheap alternative to app_browser_screenshot when you need to understand what's on the page but "
            "don't need exact pixel coordinates to click anything. Sends the screenshot to a small, unmetered "
            "image-description model instead of putting the raw image into your own context -- returns text "
            "only. Prefer this over app_browser_screenshot by default.",
            {"label": str}, app_browser_describe,
        ),
        PluginTool(
            "app_browser_evaluate",
            "Run a JavaScript expression in the labeled window's page and return its (JSON-serializable) "
            "result -- over a real CDP connection, so it works even on sites whose CSP blocks ordinary "
            "script injection.",
            {"label": str, "fn": str}, app_browser_evaluate,
        ),
        PluginTool(
            "app_browser_is_visible_on_top",
            "Checks whether the labeled window is genuinely visible and unobscured right now -- useful "
            "before a coordinate-based click to confirm it will actually land in the intended window.",
            {"label": str}, app_browser_is_visible_on_top,
        ),
        PluginTool(
            "app_browser_fill_file_dialog",
            "Waits for a native Windows file-open dialog to appear (right after clicking an upload button, "
            "say) and fills in the given path(s) and confirms in one call. Multiple paths select multiple "
            "files at once. Not tied to a specific labeled window.",
            {"paths": list, "timeoutMs": int | None}, app_browser_fill_file_dialog,
        ),
        PluginTool(
            "close_app_browser",
            "Close the embedded browser window for a label you previously opened with open_app_browser.",
            {"label": str}, close_app_browser,
        ),
        PluginTool(
            "list_app_browsers",
            "List which embedded browser windows (labels) are currently open, and what URL each is on.",
            {}, list_app_browsers,
        ),
    ],
)

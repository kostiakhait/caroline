import { z } from "zod";
import { tool, createSdkMcpServer, type McpServerConfig } from "@anthropic-ai/claude-agent-sdk";
import * as cdp from "./appBrowserCdp.js";
import { describeImageCheap } from "./voice.js";

/**
 * Caroline's own embedded multi-window browser -- one persistent, labeled
 * WebView2 window per site (e.g. "whatsapp", "telegram", "facebook",
 * "slack"), living inside the app instead of a separate standalone
 * Chromium process. See AppBrowserHost.cs/AppBrowserWindow.xaml.cs (WPF
 * side) for window lifecycle/screenshots/real-input, and appBrowserCdp.ts
 * for page-content operations (snapshot/find/click/type/press_key/
 * evaluate) -- those go over a real CDP connection to the WebView2
 * instance's own remote-debugging port, NOT through the HTTP bridge below,
 * since CDP isn't subject to the page's own CSP the way ExecuteScriptAsync-
 * injected script is (confirmed live 2026-08-31: some sites, ChatGPT and
 * Facebook among them, silently blocked ALL script injection).
 *
 * This is the PRIMARY browsing tool -- see policies.ts's
 * embeddedBrowserInstruction() for the steering that tells Caroline to
 * prefer it and fall back to the standalone caroline-browser/other browser
 * MCP servers only with the user's explicit go-ahead.
 */
const APP_BROWSER_HOST = "http://127.0.0.1:8767";

// /open no longer blocks on a full page load (see AppBrowserHost.cs's Dispatch(/open) --
// it fires StartNavigateAsync, not the blocking NavigateAsync), but a cold WebView2
// environment for a brand-new profile can still take a while to spin up its own runtime
// process, especially under system load -- confirmed live (2026-08-31) that this alone
// exceeded a 45s timeout even though the window itself should now appear immediately.
const DEFAULT_TIMEOUT_MS = 45_000;
const OPEN_TIMEOUT_MS = 90_000;

async function call(path: string, body: unknown, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<{ status: number; json: unknown }> {
  const res = await fetch(`${APP_BROWSER_HOST}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body ?? {}),
    signal: AbortSignal.timeout(timeoutMs),
  });
  const json = await res.json().catch(() => ({}));
  return { status: res.status, json };
}

/** Every CDP-routed tool needs the label's window to exist first (previously
 *  implicit -- every AppBrowserHost op used to lazily create the window;
 *  now that these ops bypass that HTTP bridge entirely, this replicates the
 *  same "just works, opens the window if needed" behavior). */
async function getCdpPort(label: string): Promise<number> {
  const got = await call("/get_port", { label });
  if (got.status === 200) return (got.json as { cdpPort: number }).cdpPort;
  console.error(`[caroline] [appbrowser] getCdpPort: label=${label} not open yet, opening lazily`);
  const opened = await call("/open", { label }, OPEN_TIMEOUT_MS);
  return (opened.json as { cdpPort: number }).cdpPort;
}

function textResult(json: unknown): { content: [{ type: "text"; text: string }] } {
  return { content: [{ type: "text", text: typeof json === "string" ? json : JSON.stringify(json) }] };
}

function errorResult(err: unknown): { content: [{ type: "text"; text: string }]; isError: true } {
  const message = err instanceof Error ? err.message : String(err);
  // Per explicit instruction (2026-09-06): this was returned to the model
  // (which sees it in her own conversation) but NEVER written to
  // caroline.log -- every app_browser_* tool failure (12+ call sites, all
  // routed through here) was completely invisible from the log alone. The
  // matching "[tool:app_browser_X] ..." entry line, logged right before the
  // call that led here, gives enough context via timestamp proximity.
  console.error(`[caroline] [tool:app_browser_*] failed: ${message}`, err);
  return {
    content: [{
      type: "text",
      text: `Embedded browser call failed: ${message}. Is Caroline's WPF app running (this tool only works ` +
        "inside the desktop app, not headless)? If the problem persists, the standalone caroline-browser " +
        "tools remain available as a fallback.",
    }],
    isError: true,
  };
}

const labelParam = z.string().describe(
  'A short identifier for which embedded window to use, e.g. "whatsapp", "telegram", "facebook", "slack". ' +
    "Each label gets its own persistent window with its own login/cookie state -- reuse the same label to " +
    "keep working in the same window, use a new one to open a separate app in its own window."
);

export function createAppBrowserTool(): McpServerConfig {
  const openAppBrowser = tool(
    "open_app_browser",
    "Open (or focus, if already open) Caroline's own embedded browser window for a given label, optionally " +
      "navigating it to a URL. This is her PRIMARY browser -- prefer it over the standalone caroline-browser " +
      "tools for ordinary web/app tasks (WhatsApp Web, Telegram Web, Facebook, Slack, general browsing). Each " +
      "label is its own persistent window living inside the app, not a separate Chrome process.",
    { label: labelParam, url: z.string().optional().describe("Initial URL to navigate to, if any.") },
    async ({ label, url }) => {
      console.error(`[caroline] [tool:open_app_browser] label=${label} url=${url ?? "n/a"}`);
      try {
        const { json } = await call("/open", { label, url }, OPEN_TIMEOUT_MS);
        return textResult(json);
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  const navigate = tool(
    "app_browser_navigate",
    "Navigate an already-open embedded browser window (see open_app_browser) to a new URL.",
    { label: labelParam, url: z.string() },
    async ({ label, url }) => {
      console.error(`[caroline] [tool:app_browser_navigate] label=${label} url=${url}`);
      try {
        const { json } = await call("/navigate", { label, url });
        return textResult(json);
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  const snapshot = tool(
    "app_browser_snapshot",
    "Tag every visible interactive element in the labeled window with a stable ref (e1, e2, ...) and return " +
      "{ref, role, name}. Refs are used by app_browser_click/app_browser_type and stay valid until the next " +
      "snapshot/find or a navigation. Goes over a real CDP connection (not affected by the page's own CSP).",
    { label: labelParam },
    async ({ label }) => {
      console.error(`[caroline] [tool:app_browser_snapshot] label=${label}`);
      try {
        const port = await getCdpPort(label);
        return textResult(await cdp.snapshot(port));
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  const find = tool(
    "app_browser_find",
    "Like app_browser_snapshot, but filtered to elements whose accessible name contains the given text " +
      "(case-insensitive).",
    { label: labelParam, text: z.string() },
    async ({ label, text }) => {
      console.error(`[caroline] [tool:app_browser_find] label=${label} text=${text}`);
      try {
        const port = await getCdpPort(label);
        return textResult(await cdp.find(port, text));
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  const click = tool(
    "app_browser_click",
    "Click an element in the labeled window by ref (from app_browser_snapshot/app_browser_find), raw CSS " +
      "selector, or raw viewport pixel coordinates (x,y -- the same coordinate space as " +
      "app_browser_screenshot's image, so you can look at a screenshot yourself and click exactly what you " +
      "see, useful when snapshot/find don't surface the element at all -- confirmed live on some messenger " +
      "web apps that render message bubbles in a way accessibility snapshots miss). x,y always does a real " +
      "OS-level click (there's no DOM element to dispatch against). For ref/selector: if a normal click " +
      "doesn't seem to register (rare now that this goes over real CDP, but some sites check event.isTrusted " +
      "even against CDP-driven input -- WhatsApp Web among them), retry with real:true: this moves the actual " +
      "system cursor and sends a real OS-level click, indistinguishable from a human click. Real clicks have " +
      "real side effects (physically move your mouse cursor, need this window visible/unobscured/focused) so " +
      "prefer the default (real:false, or ref/selector over x,y) first and only escalate when needed.",
    {
      label: labelParam, ref: z.string().optional(), selector: z.string().optional(),
      x: z.number().optional().describe("Viewport pixel X (from app_browser_screenshot) -- if given with y, clicks there directly instead of resolving ref/selector."),
      y: z.number().optional().describe("Viewport pixel Y (from app_browser_screenshot)."),
      real: z.boolean().optional().describe("Use a real OS-level click instead of CDP-driven. Default false. Ignored (always real) when x/y given."),
    },
    async ({ label, ref, selector, x, y, real }) => {
      console.error(`[caroline] [tool:app_browser_click] label=${label} ref=${ref ?? "n/a"} selector=${selector ?? "n/a"} x=${x ?? "n/a"} y=${y ?? "n/a"} real=${!!real}`);
      try {
        if (x !== undefined && y !== undefined) {
          // Coordinates -- always a real OS click, handled by AppBrowserHost/RealInput.
          const { json } = await call("/click", { label, x, y });
          return textResult(json);
        }
        if (real) {
          const { json } = await call("/click", { label, ref, selector, real: true });
          return textResult(json);
        }
        const port = await getCdpPort(label);
        return textResult({ result: await cdp.click(port, ref, selector) });
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  const scroll = tool(
    "app_browser_scroll",
    "Scrolls the labeled window with a real OS-level mouse wheel, aimed at a specific point -- either an " +
      "element (ref/selector) or raw viewport coordinates (x,y, from app_browser_screenshot). Fixes a real " +
      "problem with page-level scrolling in SPAs: focus wanders unpredictably, so PageUp/PageDown often hits " +
      "the wrong pane; aiming by cursor position (what a real wheel scroll does) sidesteps that. clicks is " +
      "signed like a physical wheel notch: negative scrolls down (toward newer content), positive scrolls up.",
    {
      label: labelParam, ref: z.string().optional(), selector: z.string().optional(),
      x: z.number().optional().describe("Viewport pixel X -- alternative to ref/selector."),
      y: z.number().optional().describe("Viewport pixel Y -- alternative to ref/selector."),
      clicks: z.number().int().optional().describe("Wheel notches, signed (negative = scroll down/toward newer content). Default -3."),
    },
    async ({ label, ref, selector, x, y, clicks }) => {
      console.error(`[caroline] [tool:app_browser_scroll] label=${label} ref=${ref ?? "n/a"} selector=${selector ?? "n/a"} x=${x ?? "n/a"} y=${y ?? "n/a"} clicks=${clicks ?? "default"}`);
      try {
        const { json } = await call("/scroll", { label, ref, selector, x, y, clicks });
        return textResult(json);
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  const type = tool(
    "app_browser_type",
    "Type text into an element in the labeled window by ref or CSS selector, over a real CDP connection. Same " +
      "real:true escalation as app_browser_click, for sites that reject even CDP-driven input on protected " +
      "fields -- this clicks the element for real to focus it, then sends real OS-level keystrokes.",
    { label: labelParam, ref: z.string().optional(), selector: z.string().optional(), text: z.string(), real: z.boolean().optional().describe("Use real OS-level input instead of CDP-driven. Default false.") },
    async ({ label, ref, selector, text, real }) => {
      console.error(`[caroline] [tool:app_browser_type] label=${label} ref=${ref ?? "n/a"} selector=${selector ?? "n/a"} textLen=${text.length} real=${!!real}`);
      try {
        if (real) {
          const { json } = await call("/type", { label, ref, selector, text, real: true });
          return textResult(json);
        }
        const port = await getCdpPort(label);
        return textResult({ result: await cdp.type(port, text, ref, selector) });
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  const pressKey = tool(
    "app_browser_press_key",
    'Press a key on the currently-focused element in the labeled window, e.g. "Enter", or "Control+A" for a ' +
      "combo -- over a real CDP connection by default, which DOES trigger a browser's native default action " +
      "for the key (unlike the old JS-dispatch approach, e.g. this will submit a form on Enter). Pass " +
      "real:true for an actual OS-level keystroke (SendInput) instead, for the rare case CDP-driven input " +
      "itself isn't accepted.",
    { label: labelParam, key: z.string(), real: z.boolean().optional().describe("Use a real OS-level keystroke instead of CDP-driven. Default false.") },
    async ({ label, key, real }) => {
      console.error(`[caroline] [tool:app_browser_press_key] label=${label} key=${key} real=${!!real}`);
      try {
        if (real) {
          const { json } = await call("/press_key", { label, key, real: true });
          return textResult(json);
        }
        const port = await getCdpPort(label);
        return textResult(await cdp.pressKey(port, key));
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  const screenshot = tool(
    "app_browser_screenshot",
    "Capture the labeled window's current page as a PNG (already zoomed out by default and capped to a " +
      "reasonable width, so a plain call is already reasonably cheap -- no need to fight the defaults). " +
      "Optional x/y/width/height crop a sub-rectangle out of the captured bitmap (in that bitmap's own pixel " +
      "space); maxWidth overrides the default cap, downscaling proportionally instead if the result is wider " +
      "than that (pass a larger value if you genuinely need more detail than the default gives you). Prefer " +
      "a crop over the full page whenever you already know roughly where the thing you need is -- it's still " +
      "cheaper than even the default-capped full frame.",
    {
      label: labelParam,
      x: z.number().int().optional().describe("Crop: left edge, in the captured bitmap's own pixel space. Requires y/width/height too."),
      y: z.number().int().optional().describe("Crop: top edge, in the captured bitmap's own pixel space. Requires x/width/height too."),
      width: z.number().int().positive().optional().describe("Crop: width in pixels. Requires x/y/height too."),
      height: z.number().int().positive().optional().describe("Crop: height in pixels. Requires x/y/width too."),
      maxWidth: z.number().int().positive().optional().describe("Downscale proportionally if the (post-crop) image is wider than this."),
    },
    async ({ label, x, y, width, height, maxWidth }) => {
      console.error(`[caroline] [tool:app_browser_screenshot] label=${label} crop=${x !== undefined ? `${x},${y},${width},${height}` : "none"} maxWidth=${maxWidth ?? "n/a"}`);
      try {
        const { json } = await call("/screenshot", { label, x, y, width, height, maxWidth });
        const imageBase64 = (json as { imageBase64?: string })?.imageBase64;
        if (!imageBase64) return errorResult(new Error("No image returned"));
        return {
          content: [
            { type: "text" as const, text: `Screenshot of ${label}` },
            { type: "image" as const, data: imageBase64, mimeType: "image/png" },
          ],
        };
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  const describe = tool(
    "app_browser_describe",
    "Cheap alternative to app_browser_screenshot when you need to understand what's on the page (has a " +
      "download finished, what does an error say, is this a login form, is this the right site...) but don't " +
      "need exact pixel coordinates to click anything. Captures the same screenshot but sends it to a small, " +
      "unmetered image-description model instead of putting the raw image into your own context -- returns " +
      "text only (a description, plus any detected objects/colors), never the image itself. Prefer this over " +
      "app_browser_screenshot by default; only reach for the real screenshot when you actually need to see " +
      "pixels yourself (e.g. to click by x,y) or this description turns out to not be enough.",
    { label: labelParam },
    async ({ label }) => {
      console.error(`[caroline] [tool:app_browser_describe] label=${label}`);
      try {
        const { json } = await call("/screenshot", { label });
        const imageBase64 = (json as { imageBase64?: string })?.imageBase64;
        if (!imageBase64) return errorResult(new Error("No image returned"));
        const described = await describeImageCheap(imageBase64);
        return { content: [{ type: "text" as const, text: JSON.stringify(described) }] };
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  const evaluate = tool(
    "app_browser_evaluate",
    "Run a JavaScript expression in the labeled window's page and return its (JSON-serializable) result -- " +
      "over a real CDP connection, so it works even on sites whose CSP blocks ordinary script injection.",
    { label: labelParam, fn: z.string().describe("A JS expression, e.g. \"document.title\" or an IIFE.") },
    async ({ label, fn }) => {
      console.error(`[caroline] [tool:app_browser_evaluate] label=${label} fn=${fn.slice(0, 200)}`);
      try {
        const port = await getCdpPort(label);
        return textResult(await cdp.evaluate(port, fn));
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  const isVisibleOnTop = tool(
    "app_browser_is_visible_on_top",
    "Checks whether the labeled window is genuinely visible and unobscured right now (not covered by another " +
      "window at its own center point) -- useful before a coordinate-based click (x,y on app_browser_click) " +
      "to confirm it will actually land in the intended window, when several labeled windows might overlap " +
      "on screen.",
    { label: labelParam },
    async ({ label }) => {
      console.error(`[caroline] [tool:app_browser_is_visible_on_top] label=${label}`);
      try {
        const { json } = await call("/is_visible_on_top", { label });
        return textResult(json);
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  const fillFileDialog = tool(
    "app_browser_fill_file_dialog",
    "Waits for a native Windows file-open dialog to appear (right after clicking an upload button, say) and " +
      "fills in the given path(s) and confirms in one call, instead of separately finding the dialog window, " +
      "finding its filename field, typing, and pressing Enter. Multiple paths select multiple files at once. " +
      "Not tied to a specific labeled window -- the dialog is its own top-level OS window.",
    {
      paths: z.array(z.string()).min(1).describe("Absolute local file path(s) to select."),
      timeoutMs: z.number().int().optional().describe("How long to wait for the dialog to appear. Default 10000."),
    },
    async ({ paths, timeoutMs }) => {
      console.error(`[caroline] [tool:app_browser_fill_file_dialog] paths=${JSON.stringify(paths)} timeoutMs=${timeoutMs ?? "default"}`);
      try {
        const { json } = await call("/fill_file_dialog", { paths, timeoutMs });
        return textResult(json);
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  const closeAppBrowser = tool(
    "close_app_browser",
    "Close the embedded browser window for a label you previously opened with open_app_browser.",
    { label: labelParam },
    async ({ label }) => {
      console.error(`[caroline] [tool:close_app_browser] label=${label}`);
      try {
        const { json } = await call("/close", { label });
        return textResult(json);
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  const listAppBrowsers = tool(
    "list_app_browsers",
    "List which embedded browser windows (labels) are currently open, and what URL each is on.",
    {},
    async () => {
      console.error(`[caroline] [tool:list_app_browsers] invoked`);
      try {
        const res = await fetch(`${APP_BROWSER_HOST}/list`, { signal: AbortSignal.timeout(10_000) });
        const json = await res.json().catch(() => ([]));
        return textResult(json);
      } catch (err) {
        return errorResult(err);
      }
    },
  );

  return createSdkMcpServer({
    name: "caroline-appbrowser",
    tools: [openAppBrowser, navigate, snapshot, find, click, scroll, type, pressKey, screenshot, describe, evaluate, isVisibleOnTop, fillFileDialog, closeAppBrowser, listAppBrowsers],
  });
}

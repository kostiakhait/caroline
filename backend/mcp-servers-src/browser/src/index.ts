import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import type { Page } from "playwright-core";
import { getBrowser, getPage, killDaemon, type DaemonConfig } from "./daemon.js";

// --- CLI config: --port <n> --user-data-dir <path> [--label <name>] ---------------------
function argValue(flag: string): string | undefined {
  const i = process.argv.indexOf(flag);
  return i >= 0 ? process.argv[i + 1] : undefined;
}
const cfg: DaemonConfig = {
  port: Number(argValue("--port") ?? 9322),
  userDataDir: argValue("--user-data-dir") ?? "C:/Users/khait/.mcp-browser-profile",
};
const label = argValue("--label") ?? "browser";

// --- ref-based element targeting -----------------------------------------------------------
// No custom native accessibility engine here (unlike @playwright/mcp's proprietary aria-ref
// snapshot). Instead, snapshot()/find() tag matching live DOM elements with a
// data-mcp-ref="e<n>" attribute and hand back {ref, role, name}; click/type resolve a ref
// straight to the CSS selector `[data-mcp-ref="..."]`. Refs are only valid until the next
// snapshot/find call (the DOM gets re-tagged each time) or a navigation.
interface TaggedEl {
  ref: string;
  role: string;
  name: string;
  tag: string;
}

// Passed directly to page.evaluate() as a real function reference, not a string. (A string
// containing an arrow-function literal evaluates to the function *value* itself, not the
// result of calling it -- page.evaluate("() => {...}") never actually invokes the function, it
// just returns the (unclonable) function object back over CDP as undefined. Cost a debugging
// round-trip to catch; passing the function directly sidesteps the ambiguity entirely.)
function tagVisibleElements(): TaggedEl[] {
  document.querySelectorAll("[data-mcp-ref]").forEach((el) => el.removeAttribute("data-mcp-ref"));
  const isVisible = (el: Element) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    const style = getComputedStyle(el);
    return style.visibility !== "hidden" && style.display !== "none" && +style.opacity !== 0;
  };
  const roleOf = (el: Element): string | null => {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === "a" && el.hasAttribute("href")) return "link";
    if (tag === "button") return "button";
    if (tag === "input") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (["button", "submit", "reset"].includes(type)) return "button";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      return "textbox";
    }
    if (tag === "textarea") return "textbox";
    if (tag === "select") return "combobox";
    if ((el as HTMLElement).isContentEditable) return "textbox";
    return null;
  };
  const nameOf = (el: Element) => {
    return (
      el.getAttribute("aria-label") ||
      el.getAttribute("placeholder") ||
      el.getAttribute("alt") ||
      el.getAttribute("title") ||
      (el as HTMLInputElement).value ||
      el.textContent ||
      ""
    )
      .trim()
      .replace(/\s+/g, " ")
      .slice(0, 160);
  };
  const candidates = document.querySelectorAll(
    'a,button,input,textarea,select,[role],[contenteditable="true"],[onclick],summary'
  );
  const results: TaggedEl[] = [];
  let i = 0;
  candidates.forEach((el) => {
    if (!isVisible(el)) return;
    const role = roleOf(el);
    if (!role) return;
    const ref = "e" + ++i;
    el.setAttribute("data-mcp-ref", ref);
    results.push({ ref, role, name: nameOf(el), tag: el.tagName.toLowerCase() });
  });
  return results;
}

async function tagPage(page: Page): Promise<TaggedEl[]> {
  return page.evaluate(tagVisibleElements);
}

function refSelector(ref: string): string {
  return `[data-mcp-ref="${ref}"]`;
}

async function resolveTarget(page: Page, opts: { ref?: string; selector?: string }) {
  if (opts.ref) return page.locator(refSelector(opts.ref)).first();
  if (opts.selector) return page.locator(opts.selector).first();
  throw new Error("Provide either ref (from browser_snapshot/browser_find) or selector.");
}

// --- click/type with a JS-dispatch fallback -------------------------------------------------
// Lesson learned the hard way running @playwright/mcp against real-world SPAs (LinkedIn's own
// composer, message box): actionability-check-based click()/fill() sometimes hangs even though
// the element is visible/enabled/stable, and separately, some React-controlled contenteditable
// widgets only update their internal state on real keydown/input events, not on a
// programmatic .value= / fill(). Both fallbacks below are exactly the workarounds that had to
// be done by hand, repeatedly, this session -- baked in here so callers don't have to.
async function clickWithFallback(page: Page, locator: ReturnType<Page["locator"]>): Promise<string> {
  try {
    await locator.click({ timeout: 6000 });
    return "clicked";
  } catch {
    await locator.evaluate((el: Element) => {
      el.scrollIntoView({ block: "center" });
      const r = el.getBoundingClientRect();
      const opts = { bubbles: true, cancelable: true, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2 };
      for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
        el.dispatchEvent(new MouseEvent(type, opts));
      }
    });
    return "clicked (JS-dispatch fallback; native click() timed out)";
  }
}

async function typeWithFallback(page: Page, locator: ReturnType<Page["locator"]>, text: string): Promise<string> {
  try {
    await locator.fill(text, { timeout: 6000 });
    return "typed (fill)";
  } catch {
    // fall through
  }
  await locator.evaluate((el: Element) => (el as HTMLElement).focus());
  await page.keyboard.press("Control+A").catch(() => {});
  await page.keyboard.press("Delete").catch(() => {});
  await page.keyboard.insertText(text);
  return "typed (focus + keyboard.insertText fallback)";
}

// --- MCP server ------------------------------------------------------------------------------
const server = new McpServer({ name: `mcp-${label}`, version: "1.0.0" });

server.registerTool(
  "browser_navigate",
  {
    title: "Navigate",
    description: "Navigate the persistent browser tab to a URL.",
    inputSchema: { url: z.string() },
  },
  async ({ url }) => {
    const { page } = await getPage(cfg);
    await page.goto(url, { waitUntil: "domcontentloaded" });
    return { content: [{ type: "text" as const, text: `Navigated to ${page.url()}` }] };
  }
);

server.registerTool(
  "browser_snapshot",
  {
    title: "Snapshot",
    description:
      "Tag every visible interactive element on the current page with a stable ref (e1, e2, ...) and return {ref, role, name}. Refs are used by browser_click/browser_type/browser_evaluate and stay valid until the next snapshot/find or a navigation.",
    inputSchema: {},
  },
  async () => {
    const { page } = await getPage(cfg);
    const tagged = await tagPage(page);
    return { content: [{ type: "text" as const, text: JSON.stringify(tagged, null, 2) }] };
  }
);

server.registerTool(
  "browser_find",
  {
    title: "Find",
    description:
      "Like browser_snapshot, but filtered to elements whose accessible name contains the given text (case-insensitive).",
    inputSchema: { text: z.string() },
  },
  async ({ text }) => {
    const { page } = await getPage(cfg);
    const tagged = await tagPage(page);
    const needle = text.toLowerCase();
    const matches = tagged.filter((t) => t.name.toLowerCase().includes(needle));
    return { content: [{ type: "text" as const, text: JSON.stringify(matches, null, 2) }] };
  }
);

server.registerTool(
  "browser_click",
  {
    title: "Click",
    description: "Click an element by ref (from browser_snapshot/browser_find) or raw CSS selector.",
    inputSchema: { ref: z.string().optional(), selector: z.string().optional() },
  },
  async ({ ref, selector }) => {
    const { page } = await getPage(cfg);
    const locator = await resolveTarget(page, { ref, selector });
    const result = await clickWithFallback(page, locator);
    return { content: [{ type: "text" as const, text: result }] };
  }
);

server.registerTool(
  "browser_type",
  {
    title: "Type",
    description:
      "Type text into an element by ref or CSS selector. Tries a fast programmatic fill first, falls back to real focus+keystroke simulation for React-controlled inputs that ignore fill().",
    inputSchema: { ref: z.string().optional(), selector: z.string().optional(), text: z.string() },
  },
  async ({ ref, selector, text }) => {
    const { page } = await getPage(cfg);
    const locator = await resolveTarget(page, { ref, selector });
    const result = await typeWithFallback(page, locator, text);
    return { content: [{ type: "text" as const, text: result }] };
  }
);

server.registerTool(
  "browser_press_key",
  {
    title: "Press key",
    description: 'Press a key or chord on the focused element, e.g. "Enter", "Control+A".',
    inputSchema: { key: z.string() },
  },
  async ({ key }) => {
    const { page } = await getPage(cfg);
    await page.keyboard.press(key);
    return { content: [{ type: "text" as const, text: `pressed ${key}` }] };
  }
);

server.registerTool(
  "browser_take_screenshot",
  {
    title: "Screenshot",
    description: "Capture the current page (viewport or full page) as a PNG.",
    inputSchema: { fullPage: z.boolean().optional() },
  },
  async ({ fullPage }) => {
    const { page } = await getPage(cfg);
    const buffer = await page.screenshot({ fullPage: fullPage ?? false, type: "png" });
    return {
      content: [
        { type: "text" as const, text: `Screenshot of ${page.url()}` },
        { type: "image" as const, data: buffer.toString("base64"), mimeType: "image/png" },
      ],
    };
  }
);

server.registerTool(
  "browser_evaluate",
  {
    title: "Evaluate",
    description:
      "Run a JavaScript function in the page. Pass ref to run it against that element instead of the page.",
    inputSchema: { fn: z.string().describe("() => {...} or (element) => {...} when ref is given"), ref: z.string().optional() },
  },
  async ({ fn, ref }) => {
    const { page } = await getPage(cfg);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const compiled = (0, eval)(`(${fn})`) as (arg?: unknown) => unknown;
    const result = ref
      ? await page.locator(refSelector(ref)).first().evaluate(compiled as never)
      : await page.evaluate(compiled as never);
    return { content: [{ type: "text" as const, text: JSON.stringify(result ?? null) }] };
  }
);

server.registerTool(
  "browser_run_code_unsafe",
  {
    title: "Run Playwright code (unsafe)",
    description:
      "Escape hatch: run arbitrary Playwright code with (page, context, browser) in scope. RCE-equivalent against this Node process -- same trust level as browser_evaluate but with full Playwright API access (locators, waitForX, multi-step flows) rather than only in-page JS.",
    inputSchema: { code: z.string().describe("async (page, context, browser) => { ... return value }") },
  },
  async ({ code }) => {
    const { page, context, browser } = await getPage(cfg);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const fn = (0, eval)(`(${code})`) as (p: unknown, c: unknown, b: unknown) => unknown;
    const result = await fn(page, context, browser);
    return { content: [{ type: "text" as const, text: typeof result === "string" ? result : JSON.stringify(result ?? null) }] };
  }
);

server.registerTool(
  "browser_tabs",
  {
    title: "Tabs",
    description: "List, open, close, or select browser tabs.",
    inputSchema: {
      action: z.enum(["list", "new", "close", "select"]),
      url: z.string().optional(),
      index: z.number().int().optional(),
    },
  },
  async ({ action, url, index }) => {
    const { context } = await getPage(cfg);
    if (action === "list") {
      const pages = context.pages();
      const list = pages.map((p, i) => `${i}: ${p.url()}`);
      return { content: [{ type: "text" as const, text: list.join("\n") || "(no tabs)" }] };
    }
    if (action === "new") {
      const page = await context.newPage();
      if (url) await page.goto(url, { waitUntil: "domcontentloaded" });
      return { content: [{ type: "text" as const, text: `Opened tab ${context.pages().length - 1}: ${page.url()}` }] };
    }
    const pages = context.pages();
    if (index === undefined || !pages[index]) {
      return { isError: true, content: [{ type: "text" as const, text: `No tab at index ${index}` }] };
    }
    if (action === "close") {
      await pages[index].close();
      return { content: [{ type: "text" as const, text: `Closed tab ${index}` }] };
    }
    // select: bring to front so subsequent tool calls that just grab pages()[0] still work
    // reasonably; getPage() always uses the first non-closed page, so make that this one.
    await pages[index].bringToFront();
    return { content: [{ type: "text" as const, text: `Selected tab ${index}: ${pages[index].url()}` }] };
  }
);

server.registerTool(
  "browser_file_upload",
  {
    title: "Upload files",
    description: "Set files on the page's currently pending file chooser (call right after clicking an upload button).",
    inputSchema: { paths: z.array(z.string()) },
  },
  async ({ paths }) => {
    const { page } = await getPage(cfg);
    const [chooser] = await Promise.all([
      page.waitForEvent("filechooser", { timeout: 5000 }),
    ]);
    await chooser.setFiles(paths);
    return { content: [{ type: "text" as const, text: `Set ${paths.length} file(s)` }] };
  }
);

server.registerTool(
  "browser_resize",
  {
    title: "Resize",
    description: "Resize the browser viewport.",
    inputSchema: { width: z.number().int(), height: z.number().int() },
  },
  async ({ width, height }) => {
    const { page } = await getPage(cfg);
    await page.setViewportSize({ width, height });
    return { content: [{ type: "text" as const, text: `Resized to ${width}x${height}` }] };
  }
);

server.registerTool(
  "browser_wait_for",
  {
    title: "Wait",
    description: "Wait for text to appear on the page, or a fixed number of milliseconds.",
    inputSchema: { text: z.string().optional(), timeMs: z.number().int().optional() },
  },
  async ({ text, timeMs }) => {
    const { page } = await getPage(cfg);
    if (text) {
      await page.getByText(text).first().waitFor({ timeout: 30_000 });
      return { content: [{ type: "text" as const, text: `Found text: ${text}` }] };
    }
    await page.waitForTimeout(timeMs ?? 1000);
    return { content: [{ type: "text" as const, text: `Waited ${timeMs ?? 1000}ms` }] };
  }
);

server.registerTool(
  "browser_restart_daemon",
  {
    title: "Restart browser daemon",
    description:
      "Recovery escape hatch: force-kill the persistent Chromium process for this profile and let the next tool call relaunch a fresh one. The on-disk profile (cookies, logins) survives. Use this if the browser seems wedged and normal tool calls keep failing.",
    inputSchema: {},
  },
  async () => {
    await killDaemon(cfg);
    await getBrowser(cfg); // relaunch immediately so the next call doesn't pay the ~2-5s cold start
    return { content: [{ type: "text" as const, text: "Daemon restarted." }] };
  }
);

const transport = new StdioServerTransport();
await server.connect(transport);

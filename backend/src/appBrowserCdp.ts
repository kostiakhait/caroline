import { chromium, type Browser, type Page, type Download } from "playwright-core";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join, extname, basename } from "node:path";

/**
 * Real CDP client for AppBrowserWindow's page-content operations
 * (snapshot/find/click/type/press_key/evaluate) -- connects directly to
 * the WebView2 instance's own remote-debugging port (see
 * AppBrowserWindow.xaml.cs's CdpPort/--remote-debugging-port) instead of
 * routing through AppBrowserHost's HTTP bridge and ExecuteScriptAsync.
 *
 * Why this exists: confirmed live (2026-08-31) that ExecuteScriptAsync-
 * injected script is silently blocked by page CSP on some sites (ChatGPT,
 * Facebook -- even a trivial `() => 42` failed there). CDP's own
 * Runtime.evaluate (which playwright-core's page.evaluate() uses under the
 * hood) is injected at the DevTools-protocol level, not as page-context
 * script, so it is NOT subject to the page's own CSP the way a `<script>`-
 * equivalent injection is -- the same reason Playwright/Puppeteer
 * automation generally works against CSP-strict sites where naive script
 * injection doesn't.
 *
 * The tagging/click/type approach here deliberately mirrors MCP/browser/
 * src/index.ts's own (data-mcp-ref attributes, JS event-dispatch fallback
 * for React-controlled inputs) -- same technique, just reached over a real
 * CDP connection to an embedded WebView2 instead of a standalone spawned
 * Chromium process.
 */

interface TaggedEl {
  ref: string;
  role: string;
  name: string;
  tag: string;
}

// Passed directly to page.evaluate() as a real function reference -- a string
// containing an arrow-function literal would evaluate to the function VALUE,
// not the result of calling it (same gotcha MCP/browser/src/index.ts's own
// copy of this documents).
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
    ).trim().replace(/\s+/g, " ").slice(0, 160);
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

function refSelector(ref: string): string {
  return `[data-mcp-ref="${ref}"]`;
}

async function resolveTarget(page: Page, opts: { ref?: string; selector?: string }) {
  if (opts.ref) return page.locator(refSelector(opts.ref)).first();
  if (opts.selector) return page.locator(opts.selector).first();
  throw new Error("Provide either ref (from app_browser_snapshot/app_browser_find) or selector.");
}

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
  await page.keyboard.press("Control+A").catch((err) => console.error("[caroline] [appbrowser-cdp] typeWithFallback: Control+A select-all failed (ignored):", err));
  await page.keyboard.press("Delete").catch((err) => console.error("[caroline] [appbrowser-cdp] typeWithFallback: Delete failed (ignored):", err));
  await page.keyboard.insertText(text);
  return "typed (focus + keyboard.insertText fallback)";
}

// --- connection management --------------------------------------------------------------------
// One cached Browser (+ its first Page) per CDP port, reused across calls --
// same reasoning as MCP/browser/daemon.ts's own cache: reconnecting fresh
// every call would be needlessly slow.
interface Cached { browser: Browser; page: Page }
const cache = new Map<number, Cached>();

async function getPage(cdpPort: number): Promise<Page> {
  const existing = cache.get(cdpPort);
  if (existing && existing.browser.isConnected() && !existing.page.isClosed()) {
    return existing.page;
  }
  console.error(`[caroline] [appbrowser-cdp] connecting to CDP port ${cdpPort}...`);
  // Brief retry -- the WebView2 instance's CDP listener may not be up yet
  // the very first moment after EnsureInitializedAsync starts (cold start).
  const deadline = Date.now() + 15_000;
  let lastErr: unknown;
  while (Date.now() < deadline) {
    try {
      const browser = await chromium.connectOverCDP(`http://127.0.0.1:${cdpPort}`, { timeout: 5_000 });
      const context = browser.contexts()[0] ?? (await browser.newContext());
      const pages = context.pages();
      const page = pages.find((p) => !p.isClosed()) ?? (await context.newPage());
      // Per explicit correction (2026-09-04): the 2026-09-01 fix below (see the
      // "downloads" section) only ever caught a download triggered by click() --
      // anything else that can start one (app_browser_navigate hitting a direct
      // file URL, pressKey submitting a form, a real OS-level click routed through
      // AppBrowserHost.cs's RealInput instead of this CDP click() at all) still fell
      // through into Playwright's own ephemeral %TEMP%\playwright-artifacts-...
      // staging with nothing ever moving it out. A single persistent listener on
      // the page itself, attached once right here (this code path only runs once
      // per distinct page object -- the cache check above short-circuits every
      // later call against the same page), covers every trigger uniformly. click()
      // below still gets its own informative "downloaded to X" return message by
      // awaiting THIS handler's own save (via downloadSavePaths), not by saving a
      // second time itself -- Playwright download data can only be consumed once.
      page.on("download", (download) => {
        downloadSavePaths.set(download, saveDownload(download));
      });
      cache.set(cdpPort, { browser, page });
      console.error(`[caroline] [appbrowser-cdp] connected to CDP port ${cdpPort}`);
      return page;
    } catch (err) {
      lastErr = err;
      await new Promise((r) => setTimeout(r, 500));
    }
  }
  throw new Error(`Could not connect to WebView2 CDP port ${cdpPort} after 15s: ${lastErr instanceof Error ? lastErr.message : String(lastErr)}`);
}

async function tagPage(page: Page): Promise<TaggedEl[]> {
  return page.evaluate(tagVisibleElements);
}

// --- downloads ---------------------------------------------------------------------------------
// Confirmed live (2026-09-01): a CDP connection makes Playwright silently
// intercept every browser download and stage it under
// %TEMP%\playwright-artifacts-<random>\<guid> -- no extension, no real
// filename, and never cleaned up if nothing consumes the Download object
// (76 leaked temp dirs found from past clicks that triggered a download
// this way, completely invisible in the real Downloads folder Caroline
// went looking in). Explicitly awaiting the "download" event and calling
// saveAs() ourselves, right here, moves the file out of that temp staging
// area into the real Downloads folder with its real suggested filename --
// this is the ONE place every app_browser_click-triggered download passes
// through, so fixing it here covers the whole tool, not just one site.
const DOWNLOADS_DIR = join(homedir(), "Downloads");

/** Windows Explorer's own "name (1).ext" convention -- matches what a real
 *  browser download already does on a name collision, so a page downloaded
 *  multiple times doesn't silently overwrite its own earlier copy. */
function uniqueDownloadPath(filename: string): string {
  const ext = extname(filename);
  const base = basename(filename, ext);
  let candidate = join(DOWNLOADS_DIR, filename);
  for (let n = 1; existsSync(candidate); n++) {
    candidate = join(DOWNLOADS_DIR, `${base} (${n})${ext}`);
  }
  return candidate;
}

/** Single source of truth for actually saving a Download's bytes out of Playwright's
 *  temp staging area -- see getPage()'s own page.on("download", ...) wiring above for
 *  why this is attached once, persistently, rather than per call site. Resolves to the
 *  real path it saved to (or throws), so any caller that also wants to know a download
 *  happened (see click() below) can await THIS promise instead of saving a second time. */
const downloadSavePaths = new WeakMap<Download, Promise<string>>();

async function saveDownload(download: Download): Promise<string> {
  const target = uniqueDownloadPath(download.suggestedFilename() || "download");
  console.error(`[caroline] [appbrowser-cdp] download detected (${download.suggestedFilename()}), saving to ${target}...`);
  await download.saveAs(target);
  return target;
}

export async function snapshot(cdpPort: number): Promise<TaggedEl[]> {
  const page = await getPage(cdpPort);
  console.error(`[caroline] [appbrowser-cdp] snapshot: tagging visible elements (port=${cdpPort})...`);
  const tagged = await tagPage(page);
  console.error(`[caroline] [appbrowser-cdp] snapshot: ${tagged.length} element(s)`);
  return tagged;
}

export async function find(cdpPort: number, text: string): Promise<TaggedEl[]> {
  console.error(`[caroline] [appbrowser-cdp] find: text="${text}" (port=${cdpPort})`);
  const tagged = await snapshot(cdpPort);
  const needle = text.toLowerCase();
  const matches = tagged.filter((t) => t.name.toLowerCase().includes(needle));
  console.error(`[caroline] [appbrowser-cdp] find: ${matches.length} match(es)`);
  return matches;
}

export async function click(cdpPort: number, ref?: string, selector?: string): Promise<string> {
  console.error(`[caroline] [appbrowser-cdp] click: ref=${ref} selector=${selector} (port=${cdpPort})`);
  const page = await getPage(cdpPort);
  const locator = await resolveTarget(page, { ref, selector });

  // Started BEFORE the click (Playwright's standard pattern for this) so it
  // can't miss a download that fires the instant the click resolves. Most
  // clicks aren't downloads, so this only adds real latency (up to 2s) on
  // the ones that actually are -- see the "downloads" section above for why
  // this exists.
  const downloadPromise = page.waitForEvent("download", { timeout: 2000 }).catch(() => null);
  const result = await clickWithFallback(page, locator);
  const download = await downloadPromise;

  let finalResult = result;
  if (download) {
    // The page's own persistent listener (see getPage()) is already saving this --
    // await ITS promise rather than calling saveAs() a second time here.
    try {
      const target = await downloadSavePaths.get(download)!;
      finalResult = `${result}; downloaded to ${target}`;
    } catch (err) {
      finalResult = `${result}; download started but could not be saved: ${err instanceof Error ? err.message : String(err)}`;
      console.error(`[caroline] [appbrowser-cdp] click: download save FAILED:`, err);
    }
  }
  console.error(`[caroline] [appbrowser-cdp] click: ${finalResult}`);
  return finalResult;
}

export async function type(cdpPort: number, text: string, ref?: string, selector?: string): Promise<string> {
  console.error(`[caroline] [appbrowser-cdp] type: ref=${ref} selector=${selector} text.length=${text.length} (port=${cdpPort})`);
  const page = await getPage(cdpPort);
  const locator = await resolveTarget(page, { ref, selector });
  const result = await typeWithFallback(page, locator, text);
  console.error(`[caroline] [appbrowser-cdp] type: ${result}`);
  return result;
}

export async function pressKey(cdpPort: number, key: string): Promise<string> {
  console.error(`[caroline] [appbrowser-cdp] pressKey: key=${key} (port=${cdpPort})`);
  const page = await getPage(cdpPort);
  await page.keyboard.press(key);
  console.error(`[caroline] [appbrowser-cdp] pressKey: pressed ${key}`);
  return `pressed ${key}`;
}

export async function evaluate(cdpPort: number, fn: string, ref?: string): Promise<unknown> {
  console.error(`[caroline] [appbrowser-cdp] evaluate: ref=${ref} fn.length=${fn.length} (port=${cdpPort})`);
  const page = await getPage(cdpPort);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const compiled = (0, eval)(`(${fn})`) as (arg?: unknown) => unknown;
  const result = ref
    ? await page.locator(refSelector(ref)).first().evaluate(compiled as never)
    : await page.evaluate(compiled as never);
  console.error(`[caroline] [appbrowser-cdp] evaluate: ok`);
  return result ?? null;
}

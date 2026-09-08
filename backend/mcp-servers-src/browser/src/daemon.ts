// Owns the lifecycle of a single, long-lived Chromium process per profile, reached over raw
// CDP (chrome://remote-debugging). This is the fix for the recurring instability of running
// @playwright/mcp's own bundled server once per Claude Code session: that model launches a
// fresh Firefox process per session against a *shared* persistent profile directory, and
// Firefox's profile lock (parent.lock) doesn't reliably get released on an ungraceful session
// exit (crash, force-kill, two sessions overlapping) -- the next session's launch then hangs
// and times out until someone manually deletes the stale lock file.
//
// Here there is exactly one real browser process per profile, ever. Every MCP tool call
// (from any session, sequentially) connects to it fresh over CDP; nobody launches a second
// competing process because every caller checks liveness via the CDP HTTP endpoint first.
// Chromium's own singleton-lock handling is also just more forgiving than Firefox's: a stale
// lock left by a killed process is detected and cleared automatically on next launch, so this
// class of failure mostly disappears on its own even without extra cleanup logic here.

import { spawn } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";
import { chromium, type Browser, type BrowserContext, type Page } from "playwright-core";

export interface DaemonConfig {
  port: number;
  userDataDir: string;
}

async function isAlive(port: number): Promise<boolean> {
  try {
    const res = await fetch(`http://127.0.0.1:${port}/json/version`, {
      signal: AbortSignal.timeout(1500),
    });
    return res.ok;
  } catch {
    return false;
  }
}

function launchDaemon(cfg: DaemonConfig): void {
  const exe = chromium.executablePath();
  const args = [
    `--remote-debugging-port=${cfg.port}`,
    `--remote-debugging-address=127.0.0.1`,
    `--user-data-dir=${cfg.userDataDir}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
    "--start-maximized",
    // Mirror the flags Playwright's own chromium.launch() passes under the hood. Without
    // these, a hand-rolled launch pulls in Chromium's default component extensions (background
    // pages / service workers for things like the reading-list and web-store integrations),
    // and connectOverCDP's initial auto-attach to every existing target then has to wait on
    // those service workers to finish registering -- observed adding 10+ seconds to the very
    // first connect after a cold launch.
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
    "--disable-background-networking",
    "--disable-client-side-phishing-detection",
    "--disable-default-apps",
    "--disable-sync",
    "--no-service-autorun",
    "--metrics-recording-only",
    "--password-store=basic",
    "--use-mock-keychain",
    "about:blank",
  ];
  const child = spawn(exe, args, {
    detached: true,
    stdio: "ignore",
    windowsHide: false,
  });
  // Let the daemon outlive this MCP server process (and the Claude Code session that spawned
  // it). This is the whole point: the browser must not die when a session ends or crashes.
  child.unref();
}

// Serializes concurrent ensureDaemon() calls within this process so two near-simultaneous tool
// invocations can't both decide the port is down and race to launch two Chromium processes.
let launching: Promise<void> | null = null;

export async function ensureDaemon(cfg: DaemonConfig): Promise<void> {
  if (await isAlive(cfg.port)) return;
  if (!launching) {
    launching = (async () => {
      if (await isAlive(cfg.port)) return; // someone else won the race while we were checking
      launchDaemon(cfg);
      const deadline = Date.now() + 20_000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 400));
        if (await isAlive(cfg.port)) return;
      }
      throw new Error(
        `Browser daemon on port ${cfg.port} (profile ${cfg.userDataDir}) did not become ready within 20s`
      );
    })().finally(() => {
      launching = null;
    });
  }
  await launching;
}

// Cached CDP connection, reused across tool calls within this MCP server process's lifetime
// (itself long-lived for the whole Claude Code session). Reconnects transparently if the
// daemon was restarted or the connection dropped.
let cachedBrowser: Browser | null = null;
let cachedPort: number | null = null;

export async function getBrowser(cfg: DaemonConfig): Promise<Browser> {
  await ensureDaemon(cfg);
  if (cachedBrowser && cachedPort === cfg.port && cachedBrowser.isConnected()) {
    return cachedBrowser;
  }
  // Explicit timeout -- Playwright's own default here is generous enough to matter (a CDP
  // endpoint that accepts the TCP connection but never completes the protocol handshake, e.g.
  // a daemon stuck mid-launch, would otherwise hang every single tool call that needs a page,
  // before any of index.ts's own per-operation timeouts even get a chance to apply).
  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${cfg.port}`, { timeout: 20_000 });
  cachedBrowser = browser;
  cachedPort = cfg.port;
  return browser;
}

// Per explicit instruction (2026-09-04): confirmed live that a triggered download was
// silently routed into a Playwright-managed temp folder (%TEMP%\playwright-artifacts-...)
// instead of the user's real Downloads folder -- since this browser is reached via
// connectOverCDP against a raw, hand-launched Chromium (not Playwright's own launch()/
// launchPersistentContext(), see this file's own header comment), nothing ever configured
// where Playwright should put downloads, and its default behavior for a CDP-attached
// browser is to intercept every download into that ephemeral artifacts directory until
// something explicitly calls download.saveAs() to move it out. Wiring a 'download' listener
// on every current AND future page (context's own 'page' event covers new tabs/popups) that
// saves straight to the real Downloads folder closes that gap for every browser tool call,
// not just ones that happen to already know to handle it themselves.
const DOWNLOADS_DIR = join(homedir(), "Downloads");
const contextsWithDownloadHandling = new WeakSet<BrowserContext>();

function wireDownloadHandling(context: BrowserContext): void {
  if (contextsWithDownloadHandling.has(context)) return;
  contextsWithDownloadHandling.add(context);

  const handlePage = (page: Page) => {
    page.on("download", (download) => {
      const destPath = join(DOWNLOADS_DIR, download.suggestedFilename());
      download.saveAs(destPath).catch((err) => {
        console.error(`[browser-daemon] download.saveAs(${destPath}) failed:`, err);
      });
    });
  };
  context.on("page", handlePage);
  for (const page of context.pages()) handlePage(page);
}

export async function getPage(cfg: DaemonConfig) {
  const browser = await getBrowser(cfg);
  const contexts = browser.contexts();
  const context = contexts[0] ?? (await browser.newContext());
  wireDownloadHandling(context);
  const pages = context.pages();
  const page = pages.find((p) => !p.isClosed()) ?? (await context.newPage());
  return { browser, context, page };
}

// Explicit recovery escape hatch: kill whatever is listening on the CDP port (best-effort,
// Windows-only via taskkill against the process holding the port) and let the next tool call's
// ensureDaemon() relaunch a clean instance. The on-disk profile (cookies, logins) survives.
export async function killDaemon(cfg: DaemonConfig): Promise<void> {
  cachedBrowser = null;
  cachedPort = null;
  await new Promise<void>((resolve) => {
    // Bounded even though this is itself a recovery escape hatch -- a hung netstat/taskkill
    // would otherwise strand the caller with no way out of the very tool meant to provide one.
    const timer = setTimeout(() => resolve(), 10_000);
    const netstat = spawn("cmd", [
      "/c",
      `for /f "tokens=5" %a in ('netstat -ano ^| findstr :${cfg.port} ^| findstr LISTENING') do taskkill /F /PID %a`,
    ]);
    netstat.on("close", () => { clearTimeout(timer); resolve(); });
    netstat.on("error", () => { clearTimeout(timer); resolve(); });
  });
}

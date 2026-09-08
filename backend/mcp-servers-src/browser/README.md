# mcp-browser

A browser-automation MCP server built to fix a specific, recurring problem with
`@playwright/mcp`: running it once per Claude Code session against a *shared* persistent
profile directory means every session launches its own Firefox process fighting over the same
profile lock. If a session crashes or two overlap, the lock (`parent.lock`) doesn't reliably get
released, and the next session's browser launch hangs and times out until someone manually
deletes the stale lock file. This happened repeatedly.

## How it's different

There is exactly **one real Chromium process per profile, ever**, run as a detached daemon that
outlives any single Claude Code session. Every tool call — from any session — connects to it
fresh over raw CDP (`chromium.connectOverCDP`), does its thing, and leaves the process running.
Nobody launches a second competing process, because every call checks the CDP port's liveness
first (`GET /json/version`) before deciding to launch.

Chromium's own singleton-lock handling is also just more forgiving than Firefox's Juggler-based
one: a lock left behind by a killed process gets cleared automatically on the next launch. Add
that most of this session's other `@playwright/mcp` flakiness (click/screenshot timeouts against
Firefox) traced back to the Juggler bridge specifically, and Chromium+CDP was the straightforward
choice — see the two `AskUserQuestion` answers this design was built against: Chromium over CDP,
one persistent daemon.

## Two profiles, mirroring the old setup

Registered in `.mcp.json` as `browser` (port 9322, `~/.mcp-browser-profile` — the "main",
logged-in-to-things profile) and `browser-social` (port 9323, `~/.mcp-browser-profile-social`),
matching the old `playwright` / `playwright-social` split. Logins (LinkedIn etc.) need to be
redone once in the new Chromium profile — nothing carries over automatically from the old Firefox
profiles.

## Tools

- `browser_navigate` — go to a URL
- `browser_snapshot` — tag every visible interactive element with a stable ref (`e1`, `e2`, ...)
  and return `{ref, role, name}`. No proprietary accessibility engine like `@playwright/mcp`'s —
  just a `data-mcp-ref` attribute written into the live DOM by a `page.evaluate` walk. Refs are
  valid until the next snapshot/find or a navigation.
- `browser_find` — same as snapshot, filtered to elements whose name contains the given text
- `browser_click` / `browser_type` — by `ref` or raw CSS `selector`. Both try the fast/native
  Playwright path first (`locator.click()` / `locator.fill()`) and fall back to a JS-dispatched
  synthetic event sequence (click) or focus+`keyboard.insertText` (type) if that times out or the
  page's React state doesn't pick up a programmatic fill — exactly the manual workarounds this
  session needed repeatedly against LinkedIn's own UI, now automatic.
- `browser_press_key`, `browser_take_screenshot`, `browser_resize`, `browser_wait_for`,
  `browser_tabs` (list/new/close/select), `browser_file_upload`
- `browser_evaluate` — run JS in the page, optionally scoped to a `ref`
- `browser_run_code_unsafe` — escape hatch, `async (page, context, browser) => {...}` with full
  Playwright API access. RCE-equivalent against this Node process, same trust level as
  `browser_evaluate`.
- `browser_restart_daemon` — force-kill the Chromium process for this profile (by finding the
  PID listening on the configured port, not by image name — never a blanket `taskkill /IM
  chrome.exe`, which would hit unrelated Chrome windows too) and relaunch a clean one. The
  on-disk profile survives, so cookies/logins aren't lost. Use this if the browser seems wedged.

## Build

```
npm install
npm run build
```

No native/C# layer here (unlike the `windows-*` servers) — everything is CDP over the network,
no Win32 interop needed.

## Known quirk this took a while to track down

A hand-rolled Chromium launch (just `--remote-debugging-port` + `--user-data-dir`, no other
flags) pulls in Chromium's default component extensions — background pages/service workers for
things like the reading-list and web-store integrations. `connectOverCDP`'s initial auto-attach
to every existing target then waits on those service workers to finish registering, adding
10-30+ seconds (sometimes exceeding Playwright's own 30s connect timeout) to the very first
connect after a cold launch. `daemon.ts`'s launch args mirror what `chromium.launch()` passes by
default (`--disable-extensions`, `--disable-component-extensions-with-background-pages`, etc.) to
avoid this entirely — cold connect is ~100-200ms with them in place.

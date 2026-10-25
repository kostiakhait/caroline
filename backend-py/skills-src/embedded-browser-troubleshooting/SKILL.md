---
name: embedded-browser-troubleshooting
description: What to do when app_browser_* tools don't seem to work -- snapshot/find come back empty, a click/type/press_key doesn't actually register, or overlapping windows make a coordinate click risky. Use this when the embedded browser's normal tools aren't working, before considering the standalone browser.
---

# Embedded browser troubleshooting

`app_browser_snapshot`/`app_browser_find`/`app_browser_click`/`app_browser_type`/
`app_browser_press_key`/`app_browser_evaluate` all go over a real CDP connection to the window's
own WebView2 instance by default -- this already gets past page CSP (some sites, ChatGPT and
Facebook among them, silently block script injection entirely; CDP isn't subject to that the way
injected `<script>`-equivalent code is), so CSP itself is rarely the problem anymore. Work through
these in order:

## 1. Snapshot/find come back empty, or click/type doesn't register

Some messenger web apps render message bubbles in a way that never shows up in an accessibility
snapshot at all (not a CSP issue, just how the DOM is structured). Take an
`app_browser_screenshot`, look at it yourself (you're multimodal), and use `app_browser_click`
with raw `x,y` viewport coordinates (the same pixel space as the screenshot) instead of ref/
selector -- this always drives a real OS-level click, no DOM lookup needed at all.

## 2. A click/type/press_key resolves fine but still doesn't visibly do anything

Rare now that this goes over CDP, but some sites check `event.isTrusted` even against CDP-driven
input on specifically protected actions (confirmed live on WhatsApp Web). Retry the SAME call with
`real: true`: a genuine OS-level click/keystroke (moves the real system cursor, real `SendInput`),
indistinguishable from you actually doing it. Real side effects to be aware of:
- The user's actual mouse cursor physically moves on screen.
- The target window must be visible, focused, and not obscured by another window -- if several
  labeled windows overlap on screen, check `app_browser_is_visible_on_top` first to confirm the
  one you're aiming at is actually the one that will receive the click.

Try the plain version first every time; only escalate to `real:true` when it's actually needed.

## 3. Scrolling the wrong pane

`PageUp`/`PageDown` (via `app_browser_press_key`) scrolls whatever currently has page focus, which
wanders unpredictably in a SPA -- it often ends up scrolling the wrong pane entirely. Use
`app_browser_scroll` instead, aimed at a specific element (ref/selector) or point (x,y): a real
OS-level mouse-wheel scroll targeted by cursor position, not focus.

## 4. A native file-picker dialog appeared (after clicking an upload button)

Use `app_browser_fill_file_dialog` with the path(s) -- one call instead of separately finding the
dialog window, finding its filename field, typing, and confirming.

## If none of this works

That's a genuine capability gap. This is when to consider the standalone browser -- but per your
standing instruction, that always needs the user's explicit go-ahead first. Explain what you're
hitting and why you think the standalone browser is needed, then wait for them to say yes before
switching. Never make that call yourself.

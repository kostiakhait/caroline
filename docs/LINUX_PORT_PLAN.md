# Port Caroline to Linux

## Context

Caroline currently only runs on Windows: the shell is WPF (`Windows/Caroline`), the embedded
browser host is WebView2-based (`Windows/Caroline.NativeHost`), the installer is a WPF bootstrapper
producing a self-contained `win-x64` exe, and a family of real-time GUI-automation tools (mouse,
keyboard, screenshot, window inspection, a linear automation "chain") are thin Python wrappers
(`backend-py/app/plugins/{mouse,keyboard,inspect,chain,screenshot,window_mouse,window_keyboard,
window_screenshot}_plugin.py`) around compiled C#/.NET helper exes that call raw Win32 APIs
(`SendInput`, `GetForegroundWindow`, `PrintWindow`, Toolhelp32Snapshot, etc. --
`backend/mcp-servers-src/*/native/*.csproj`).

A three-agent codebase audit found the picture is much better than it looks at first glance:
**backend-py itself is already ~90% portable** (paths, subprocess flags, the CDP bridge to the
embedded browser are all OS-agnostic or already no-op-safe on Linux). The real porting work
concentrates in a small number of places: the native automation exes, the WPF shell, the
WebView2-based browser host, and the installer. A large, Node.js-based `backend/mcp-servers` tree
that the Makefile still builds and ships is **confirmed dead weight** -- never spawned by the live
product (an old pre-migration artifact) -- and is dropped from the Linux build rather than ported.

## Architecture decisions (confirmed)

- **UI shell: Avalonia** (C#, the closest cross-platform analog to WPF -- most of the existing
  window/XAML logic ports with structural changes, not a rewrite into a new language). The Linux
  installer's own UI is visually identical to the Windows `CarolineInstaller` wizard, same Avalonia
  toolkit.
- **Automation target: X11-only, x86_64-only.** XTest/Xlib gives a direct analog of SendInput/
  GetForegroundWindow/PrintWindow and works transparently under XWayland too; native Wayland input
  injection/window introspection has no unified API and is out of scope. ARM64 Linux is explicitly
  deferred to a later, separate pass -- CUDA/cuDNN (local Whisper STT's GPU path) has no clean
  ARM64 story today, and doubling the build/test matrix for a currently-negligible user base isn't
  worth bundling into an already multi-phase port.
- **Embedded browser: real Playwright-launched Chromium windows, driven directly from backend-py**
  -- no separate "NativeHost" process at all on Linux. `app_browser_cdp.py` already talks to each
  window over CDP via Playwright; the only thing WebView2 added was *embedding* the window inside
  Caroline's own app chrome. A real, separate, per-label persistent Chromium window
  (`playwright.chromium.launch_persistent_context(user_data_dir=..., headless=False)`) gives the
  same "persistent, labeled, visible site window" behavior with far less new code.
- **Native automation helpers: plain Python using `python-xlib`, in-process, no separate compiled
  binary** -- simpler than writing new Linux C#/.NET console apps (one less toolchain to build/ship),
  and `native_exe.py`'s subprocess-spawn layer is simply skipped on Linux.
- **Installer distributable: a single self-contained `linux-x64` binary** (`dotnet publish -r
  linux-x64 --self-contained`), same model as the Windows zip-download bootstrap, not distro
  packaging. Packaged as an **AppImage** on top of that binary so double-click works reliably across
  GNOME/KDE/XFCE file managers (a bare downloaded Linux binary isn't executable by default -- this
  is the one real added-friction point vs. Windows that packaging smooths over, not eliminates).

## Scope and non-goals

- Target: a genuinely separate Linux build/install path, built and shipped alongside the existing
  Windows one -- not replacing it. The Windows product is unaffected.
- Out of scope: Wayland-native input/window APIs; ARM64 (see above, deferred); distro packaging
  (`.deb`/`.rpm`/Flatpak); the Android companion app (confirmed already OS-agnostic, no changes
  needed); Windows Defender-equivalent handling (no Linux analog needed); splitting the engine
  (backend-py) and the UI across two machines (discussed and rejected -- most of backend-py's own
  tools act on whichever machine it runs on, so splitting breaks the point of most of them, and
  today's loopback-only/unauthenticated trust model would need real hardening first).

## Phased plan

### Phase 1 -- backend-py portability (lowest risk, do first, unblocks testing everything else)

Mechanical fixes, each isolated and independently testable, runnable/verifiable under WSL2 (with
WSLg) without needing any of the later phases to exist yet:

- `app/workspace_dir.py` -- Linux default (`$XDG_DATA_HOME/caroline` or `~/.local/share/caroline`)
  alongside the existing `%LOCALAPPDATA%` default, chosen by `sys.platform`/`os.name`.
- `supervisor.py`: `_resolve_pythonw_exe` -- on Linux resolve a bundled `python3` (no
  windowless/console distinction exists there, collapses to one path). `_child_env` -- drop the
  `.exe` suffix for `ffmpeg`/`codex-app-server`/the Linux browser-automation entry point on
  non-Windows.
- `app/chat_session.py` `_force_kill_underlying_cli_process` (~line 2507) -- POSIX branch: kill the
  whole process group (`os.killpg(pid, signal.SIGKILL)`); confirm the subprocess spawn uses
  `start_new_session=True` on POSIX so there's an actual process group to target.
- `app/plugins/files_plugin.py` `open_file_with_default_app` -- `subprocess.Popen(["xdg-open",
  path])` on non-Windows in place of `os.startfile`; check the second call site in `app/main.py`.
- `app/local_stt.py` `_register_cuda_dll_dirs` -- already correctly no-ops on non-Windows; confirm
  CUDA/cuDNN libs are found automatically on a real Linux test run (may need `LD_LIBRARY_PATH` set
  in `_child_env` if not).
- `supervisor.py`'s existing `_kill_pid` POSIX branch (already written for this future port) --
  verify it actually works end to end; confirm `start_new_session=True` wherever `run_server.py`
  itself is spawned.

### Phase 2 -- native automation tools (X11), one plugin family at a time

Per `app/plugins/native_exe.py`'s own existing shape, each plugin resolves an OS-specific helper and
shells out to it. On Linux, skip the subprocess layer entirely: a small `app/plugins/_x11_input.py`/
`_x11_window.py` helper module using `python-xlib` directly, in-process.

- `mouse_plugin.py` -- `XTestFakeMotionEvent`/`XTestFakeButtonEvent`.
- `keyboard_plugin.py` -- `XTestFakeKeyEvent`; a parallel `Xlib.XK` keysym table replacing
  `native_exe.py`'s Win32-VK-code `_NAMED_KEYS`.
- `window_mouse_plugin.py` / `window_keyboard_plugin.py` -- `XSendEvent` to a specific window;
  test against a real target app (same "doesn't work on GPU-rendered/Electron custom controls"
  caveat the Windows version already carries).
- `inspect_plugin.py` -- `_NET_CLIENT_LIST`/Xlib tree-walking; the "hwnd" becomes an X11 window ID.
- `window_screenshot_plugin.py` -- `XGetImage` in place of `PrintWindow`.
- `screenshot_plugin.py` -- full-screen/monitor capture via Xlib root-window `XGetImage`.
- `chain_plugin.py` -- ports last, a linear combinator over the other six.
- The browser host's `IsVisibleOnTop` equivalent (Phase 3) uses the same X11 toolkit --
  `_NET_CLIENT_LIST_STACKING` hit-test at a screen point.

**Status: done (2026-10-04), verified live against a real X server (WSL2 + WSLg).** All eight
plugins (`mouse`, `keyboard`, `inspect`, `screenshot`, `window_mouse`, `window_keyboard`,
`window_screenshot`, `chain`) have a Linux branch now, each gated by `sys.platform`, Windows branch
byte-for-byte untouched (confirmed: all eight still import cleanly under a real win32 Python).
Two new helper modules: `app/plugins/_x11_input.py` (XTest mouse/keyboard + posted/XSendEvent
window-targeted input) and `app/plugins/_x11_window.py` (recursive-XQueryTree window enumeration/
geometry). `chain_plugin.py`'s Linux branch is a full Python-native reimplementation of
`chain.exe`'s interpreter loop (`app/plugins/_linux_chain.py`), not a thinner subset -- every op
(move/click/mouse_down/mouse_up/drag/key/type/scroll/sleep/wait_window/wait_pixel/wait_idle/launch/
kill/restart/checkpoint) ported and exercised live, including the retry/failedAt/screenshot result
shape matching the C# original's JSON exactly.

Real findings from live testing (not assumptions):
- **`_NET_CLIENT_LIST` is NOT set** by WSLg's own window manager -- `inspect`/`chain`/window-*
  tools' window enumeration deliberately never relies on it, using recursive `XQueryTree` +
  WM_NAME/WM_CLASS filtering instead (the same WM-independent fallback xdotool itself uses, used
  here unconditionally rather than as a fallback path).
- **This machine's X server keyboard layout is Russian** -- plain Latin keysyms (e.g. `XK_a`) have
  *no keycode at all* in the live layout, so XTest (which only takes a keycode, never a keysym)
  can't type them directly. Fixed with the same technique `xdotool type` uses: temporarily bind one
  unused keycode to whatever keysym is needed via `ChangeKeyboardMapping`
  (`_x11_input.py`'s `_keysym_to_keycode`/`_find_free_keycode`) -- confirmed this makes `type_text`/
  `press_key` layout-independent, not just a Russian-layout workaround.
- **WSLg's WM reports a sentinel `(-32768, -32768)` geometry** for a just-created top-level frame
  window for a brief, variable window after mapping (confirmed: a 1.2s settle resolves correctly
  100% of the time; shorter waits are flaky) before it settles to the real position --
  `_x11_window.get_window_rect` retries a few times with a short sleep rather than returning the
  sentinel as if it were real, and documents this as unconfirmed on a real (non-WSLg) X.Org desktop
  either way.
- Window-targeted ("posted") input uses `XSendEvent`, which marks `send_event=true` on the
  delivered event -- confirmed delivered correctly to a plain X11 window in testing, but same
  documented caveat as the Windows posted-message tools already carry: some GTK/Qt/Electron
  toolkits deliberately ignore synthetic events for that exact reason and need real XTest input
  instead.
- Screenshot capture uses `mss` (pure-Python + zlib PNG encoding via `mss.tools.to_png`, no Pillow)
  rather than hand-rolled Xlib `GetImage` -- the latter threw `BadMatch` against the real server in
  testing; `mss` worked cleanly for both full-screen and cropped capture. `maxWidth` downscaling is
  a small hand-written nearest-neighbor resize (avoids a new Pillow/numpy dependency for one rarely
  used parameter).
- **New Python dependencies this phase introduces, not yet pinned/bundled anywhere**: `python-xlib`
  and `mss`. No `requirements.txt`/pinned-deps manifest exists anywhere in this repo today --
  Phase 5 (installer/packaging) needs to land wherever/however backend-py's other dependencies get
  bundled into the shipped runtime; flagged here so it isn't missed, not resolved in this phase.
- Single-window capture (`window_screenshot_plugin.py`) and `chain_plugin.py`'s `wait_pixel`/
  `wait_idle`/`checkpoint` window modes screen-crop via the window's resolved rect rather than an
  occlusion-safe `PrintWindow` equivalent (none exists in core X11 without compositor cooperation) --
  a real, documented fidelity gap, not an oversight.

**Re-verified (2026-10-05) on a disposable Vultr cloud instance** (Ubuntu 24.04, Xvfb + openbox --
a genuine reparenting WM, not WSLg's RDP/RAIL-remoted one) after the user asked that testing move
off their own machine entirely. The full Phase 2 suite (mouse/keyboard primitives, window
enumeration, posted window input, the chain interpreter including its failure path) passed cleanly
on real X11 with no changes needed -- confirms the WSLg-specific caveats above really are WSLg-
specific, not artifacts of the implementation itself.

### Phase 3 -- embedded browser on Linux (Playwright-launched real windows)

- New `app/plugins/_linux_app_browser.py` replacing `Caroline.NativeHost.exe`/`AppBrowserHost.cs`'s
  role: `app_browser_plugin.py`'s `_call`/`_get` route here in-process on Linux instead of an HTTP
  bridge to a separate exe. `/open` -> `launch_persistent_context(user_data_dir=<per-label profile
  dir>, headless=False)`, one `(label -> context/page)` map alive for the backend process's life.
- `/navigate`, `/close`, `/list`, `/screenshot` -- direct Playwright calls, no HTTP hop needed.
- `real:true` click/type/press_key -- route to the Phase 2 X11 input helpers at the window's actual
  screen position.
- `/fill_file_dialog` -- **not needed as Win32-style UI automation at all**: Playwright's native
  `page.expect_file_chooser()` intercepts the file-input click before any OS dialog opens, a direct
  simplification over `FileDialogHelper.cs` (which has no clean Linux equivalent).
- `/is_visible_on_top` -- X11 stacking hit-test at the Playwright window's own X11 window ID.
- Drop the lazy-launch-a-separate-exe logic entirely on Linux -- nothing to launch.

**Status: done (2026-10-05), verified live** on the same disposable Vultr instance Phase 2 was
re-verified on (Ubuntu 24.04, Xvfb + openbox). `app_browser_plugin.py` got the same
`sys.platform`-gated branch treatment as every Phase 2 plugin; `app_browser_cdp.py` needed zero
changes (it was already a pure CDP-port client, platform-agnostic by construction). Each label's
Chromium launches with `--app=<url>` (no address bar/tabs/toolbar) -- the visual and positional
analog of WebView2 being embedded with no chrome on Windows, which also makes the `real:true`
OS-level click escalation's math exact (zero chrome means the OS window's rect IS the page
viewport at offset 0,0) instead of needing a toolbar-height estimate.

Real findings from live testing:
- **The exact `data:`-URL-produces-an-empty-page bug hit during initial (WSLg) testing does NOT
  reproduce on a real X11 desktop** -- confirmed by running the identical test unmodified on the
  Vultr box. Root-caused to WSLg's own remoting/compositor layer, not this code; no workaround was
  needed once testing moved off WSLg, exactly the outcome the user's "test on Vultr instead"
  instruction was aiming for.
- **A real, non-WSLg-specific bug, caught here**: `_x11_window.py`'s `_wm_name()` read only the
  legacy ICCCM `WM_NAME` property, which Chromium (and modern GUI apps generally) leaves empty --
  the actual title lives in the EWMH `_NET_WM_NAME` property (UTF8_STRING) instead. This silently
  broke every title-based window match (`app_browser`'s `is_visible_on_top`/`real_os_click`, which
  match a Playwright page to its OS window by title since X11 window properties don't otherwise
  expose which CDP port a window belongs to). Fixed by reading `_NET_WM_NAME` first, falling back
  to legacy `WM_NAME` for older clients that only set that -- this also improves `inspect_plugin.py`
  /`chain_plugin.py`'s window matching generally, not just app_browser's own use of it.
- `page.screenshot()` failed outright ("Unable to capture screenshot") against the GPU compositing
  path in both test environments (WSLg and the Vultr Xvfb box) until Chromium was launched with
  `--disable-gpu` -- kept on unconditionally for every Linux app-browser window, not just as a
  test-environment workaround, since it trades a little rendering performance for not depending on
  GPU driver state this installer can't control on an arbitrary end-user machine.
- `maxWidth` screenshot downscaling resizes the actual Playwright viewport before capturing (no
  pixel-resampling dependency needed) when no crop is requested; combined with an explicit crop in
  the same call, the crop is honored exactly and the downscale is skipped rather than attempting to
  rescale clip coordinates against a resized viewport too -- a documented simplification for an
  uncommon combination, not a silent gap.
- `is_visible_on_top` is a real, documented simplification, not full parity with the Windows
  version: it confirms the window is mapped/viewable, not that it's the actual unobscured topmost
  window at its position -- a true occlusion test needs `_NET_CLIENT_LIST_STACKING`, which (per
  Phase 2's own finding) this class of WM doesn't reliably set.

### Phase 4 -- Avalonia shell (the largest phase; do after 1-3 are independently working)

Port, not rewrite-from-scratch, each existing WPF surface:
- `MainWindow.xaml(.cs)` -- tab strip + chat tabs; evaluate Avalonia's own WebView integrations for
  `chat.html`/`chat.js` once this phase starts (separate, smaller decision than Phase 3's browser-
  automation question -- chat tabs just need rendering + a JS bridge, not CDP).
- `SplashWindow.xaml(.cs)` -- borderless/transparent, Avalonia supports this directly.
- `DocumentViewerWindow.xaml(.cs)` -- same WebView-hosting pattern as the chat tabs.
- `VisualModeWindow.xaml(.cs)` + `VisualModeManager.cs` -- borderless/topmost/per-pixel-alpha; the
  one UI surface with no exact toolkit-level equivalent (X11 override-redirect + compositor alpha
  varies) -- prototype early, highest visual risk.
- Tray icon -- DBus StatusNotifierItem, best-effort (GNOME needs the user's own extension, same as
  every Linux tray app -- not something Caroline can paper over). **Untestable under WSLg** (no
  panel/tray host exists there at all) -- needs a real desktop session.
- Global hotkey -- `XGrabKey`. Behavior under XWayland/WSLg specifically is uncertain, verify on a
  real X11 session, not just WSL.
- Autostart -- `~/.config/autostart/*.desktop` (works across DEs, chosen over a systemd user unit
  for that reason).
- Single-instance guard -- a lock file (`flock`) under the Linux workspace dir, replacing the named
  `Mutex`.
- `XcfaRenderer` (vendored `ProjectReference`) -- not yet audited for hidden Win32 P/Invoke; a real
  open question, not assumed portable.

**Status: in progress (2026-10-05).** New project `Windows/Caroline.Linux` (Avalonia 12.1.3, net8.0,
namespace `Caroline` to match the WPF project). Windows/Caroline is untouched.

Done and verified on the Vultr Ubuntu 24.04 box (Xvfb + openbox):
- `SplashWindow` (transparent, banner cycle, click to dismiss), `MainWindow` (placeholder status view
  and log box), `App` startup (single-instance guard, global exception logging).
- `Native/SupervisorClient` launches `python3 backend-py/supervisor.py` from the bundled
  `runtime/python/bin/python3`, streams its output, and polls its `/status` endpoint. Confirmed with a
  real child process and a real 200 response.
- `Services/Logger.cs` is reused unchanged: .NET resolves LocalApplicationData to `~/.local/share` on
  Linux, so the WPF file works as-is.
- Cross-compiling `linux-x64` from the Windows dev box works.

Real findings from the teardown work:
- **Killing the shell with SIGTERM left `supervisor.py` orphaned.** Investigated in order:
  - `AppDomain.ProcessExit` does fire on SIGTERM for a plain .NET console app, but does NOT fire for
    the Avalonia/X11 host.
  - `PosixSignalRegistration` for SIGTERM/SIGINT did not run either (the process exits first).
  - Fix that works: `backend-py/supervisor.py` calls `prctl(PR_SET_PDEATHSIG, SIGKILL)` on itself
    and on the backend it spawns (Linux only, no-op on Windows). The kernel then kills the child
    whenever the shell dies, including SIGKILL, which no handler can catch.
  - Verified: after SIGTERM and after SIGKILL, no supervisor process remains.
  - Not verified: the grandchild (`run_server.py`) path. It can't stay up on the test box because
    `claude_agent_sdk` isn't installable there, so that half is covered by code review only.
- Test-harness gotcha, not a product bug: `pkill -f <pattern>` run inline over SSH matches the
  remote shell's own command line and kills the session. Run such commands from a script file.

Web content, decided 2026-10-05 as "embedded Avalonia webview" (user's choice), then found not
working: tried `Avalonia.Controls.WebView` 12.1.0 (official avaloniaui package, has a WebKitGTK
adapter) with `NativeWebView` + `Source`, WebKitGTK 4.1 installed on the test box. It built, but no
WebKit process started and no content rendered, even with an explicit minimum height. The package's
own README steers Linux toward `NativeWebDialog` (a separate native window) instead of the embedded
control. Reverted; nothing committed for this.

**Pivot (2026-10-05): the Linux shell is now Python, not Avalonia.** The user chose to move the shell
to Python + WebKit2. Verified on the test box, in order:
- `pywebview` 6.2.1 and 5.4 on GTK: page and JS bridge work, but the window never maps. Dropped.
- Bare WebKit2 inside GTK 3 (`PyGObject`): window maps and renders.
- JS to Python and back through `WebKit2` script message handler: works.
- `linux-shell/caroline_shell.py`: GTK window, WebKit2 view, `window.chrome.webview` shim (the page's
  WebView2 API), local HTTP server for `Windows/Caroline/wwwroot`, supervisor start and stop. The real
  chat page loads and renders. Backend shows `reconnecting` on the test box because `claude_agent_sdk`
  isn't installable there. Supervisor cleanup on SIGTERM verified (no leftover process).

Since the pivot, also done and verified on the test box (icons/fonts aside, "maximally close to
Windows" per explicit instruction):
- **Font**: Selawik (SIL OFL 1.1, Microsoft -- same metrics as Segoe UI) bundled in
  `linux-shell/fonts/` and mapped onto the page's own `font-family: "Segoe UI"` requests via a
  WebKit user stylesheet; the page itself is unchanged.
- **Tab strip**: ports `MainWindow.xaml.cs`'s `RebuildTabStrip`/`ApplyTabModeStyle` -- one
  `WebKit2.WebView` per tab at the same `chat.html?port=...&tab=...&alwaysOnTop=...&assetsVersion=...`
  URL the WebView2 build already uses, a hamburger menu (Mode submenu/Clear/Close, same enable/
  disable rules), active-tab RoyalBlue highlight on the `#3A3F8F` strip, double-click rename. Not
  yet persisted (open tab ids/names/window position -- no `SettingsService` equivalent here).
- **Splash screen**: chrome-less, centered, cycling `SplashBanners/*.png`, dismiss logic ports
  `WaitForSplashDismissAsync` exactly (3s floor, 5min ceiling, polls the backend's own
  `/api/status` for `forcedCompactionPending`). No compositor on this test box (same as WSLg), so
  real window transparency isn't available -- falls back to a solid `#3A3F8F` panel rather than
  GTK's default light background, which otherwise left the white "Connecting..." text unreadable.
- **Viewer windows** (`linux-shell/viewer_window.py`, ports `DocumentViewerWindow`): image, video,
  code/slideshow (loads the same `monaco_viewer.html`/`slideshow.html` from the shell's own local
  HTTP server), and the SquirrelWisdom login form. The `window.chrome.webview` shim gained a real
  `addEventListener("message", ...)` + an internal `__dispatch` the native side calls, needed for
  these pages' own message listener and for posting `editor_result`/`login_result` back -- same
  shape `PostWebMessageAsJson` already produces. NOT ported: the OnlyOffice editor ("office" kind)
  and the payment checkout viewer -- both need a live backend session to exercise at all. Monaco's
  own editor body didn't render content within a short (~3s) live test; flagged as unconfirmed, not
  claimed working.
- **Visual Mode found NOT to be a simple port**: audited `vendor/XcfaRenderer` (the talking-head
  renderer `VisualModeManager.cs`/`VisualModeWindow.xaml.cs` depend on) -- its real dependencies
  (SkiaSharp, OpenCvSharp) both ship Linux builds, and the only confirmed Win32-specific code is
  `ProcessWatchdog.cs`'s small `kernel32.dll` P/Invoke. The actual blocker is architectural: XcfaRenderer
  is a C# library and the Linux shell is now Python, so using it needs either a bridge process or a
  full reimplementation -- neither exists. Resolved honestly rather than left silently broken:
  `visual_speech_audio` now replies immediately with `{type: "visual_speech_done", played: false}`,
  the exact signal `chat.js` already uses (Windows side, "model not warmed yet") to fall back to
  plain audio -- so a voice reply stays audible instead of `chat.js` hanging on a reply that would
  otherwise never arrive. `visual_mode_config`/`visual_speech_start/stop/cancel` are no-ops (no
  native reply is expected for these on Windows either).
- **Single-instance guard**: `flock(LOCK_EX | LOCK_NB)` on `<XDG_DATA_HOME>/caroline/caroline.lock`
  (the direct analog of the WPF build's named Mutex) -- released automatically by the kernel if the
  process dies without closing it, no stale-lock cleanup needed. A second launch shows the same
  "Caroline is already running." message and starts no second supervisor/backend. Verified live:
  confirmed via the shell log that only one supervisor/backend process ever started with two
  instances launched back to back.
- **Global hotkey**: `linux-shell/hotkeys.py` uses `XGrabKey` on the root window (the direct X11
  analog of `RegisterHotKey`/`WM_HOTKEY`, `GlobalHotkeyService.cs`) on a background thread (python-
  xlib's event loop blocks), marshaling callbacks onto the GTK main loop via `GLib.idle_add`. Same
  two bindings/defaults as `MainWindow.xaml.cs`: Ctrl+Alt+C toggles window visibility, Ctrl+Shift+C
  toggles voice recording on the active tab. Neither is user-configurable yet (no settings
  persistence). Verified live: two Ctrl+Alt+C presses toggled real X11 window visibility
  (`_x11_window.list_windows`) from visible to hidden and back.
- **`.desktop` file** (`linux-shell/caroline.desktop`): passes `desktop-file-validate`, and a
  substituted copy placed in `~/.local/share/applications/` actually launches the app correctly via
  `gtk-launch` (confirmed by a real new window with the right pid). `X-GNOME-Autostart-enabled=true`
  is set so the same file works for both the app-menu entry and (copied into
  `~/.config/autostart/`) autostart -- matches `Autostart.cs`'s own two roles. **Found while writing
  this**: unlike the backend (`runtime/python`, fully self-contained), the shell's own Python
  process needs the SYSTEM python3 specifically, because `python3-gi`/`gir1.2-webkit2-4.1` are
  system packages tied to the installed GTK3/WebKitGTK library versions -- not something pip-
  installable into an isolated venv/bundled runtime the way the backend's own dependencies are.
  Phase 5's installer needs to either declare these as runtime package dependencies or find another
  way to bundle them; it can't just copy a self-contained Python tree for the shell the way it does
  for the backend. Registering the `.desktop` file into those two directories at install time is
  itself still Phase 5's job (`Autostart.cs`'s own role on Windows is 100% installer-side, nothing
  in `MainWindow.xaml.cs` registers autostart itself -- mirrored here: nothing in
  `caroline_shell.py` does either).

**Always-on-top verified**: `set_always_on_top` (already wired to `window.set_keep_above`) was
confirmed live by checking `_NET_WM_STATE` via `xprop` directly -- `set_keep_above(True)` actually
adds `_NET_WM_STATE_ABOVE` on this WM (openbox), not just a no-op GTK call.

Still open: tray, and packaging itself (actually wiring the `.desktop` file into an installer,
bundling the icon it references, settings persistence). The emoji/icon-font gap (📎, 🎙️, etc.
render as boxes) is tracked separately -- drawn replacement icons exist outside the repo pending a
decision on light-background contrast, not yet wired in beyond the dark-background-only tab-strip
hamburger (`linux-shell/icons/menu.png`).

### Phase 5 -- installer/packaging

**Revised 2026-10-06 to match the pivot away from Avalonia** (see Phase 4's own "Pivot" note): the
shell is `linux-shell/*.py`, not a compiled .NET binary, so there is nothing to `dotnet publish` on
Linux at all. The bullets below replace the original (now-stale) .NET-publish-centric plan.

- No `dotnet publish` for the shell -- it's plain Python, runs via the system python3 (see Phase 4's
  own finding on why: `python3-gi`/`gir1.2-webkit2-4.1` are system packages, not something bundled
  into an isolated runtime the way the backend's own deps are). The installer's job is laying files
  down and declaring package dependencies, not compiling anything for the shell.
- AppImage packaging needs to bundle `python3-gi`/`gir1.2-webkit2-4.1`/GTK3/WebKitGTK themselves
  (not just reference them), since an AppImage can't rely on the host having matching versions
  installed -- more involved than a typical AppImage (which usually just bundles app-specific
  libs), worth validating early rather than assuming it'll "just work" like the backend's own
  self-contained runtime does.
- `runtime/python` (backend-py's own isolated interpreter, with `claude_agent_sdk`) and
  `runtime/codex` stay the same shape as Windows -- see the still-open provisioning gap noted below,
  which applies equally regardless of this pivot.
- Drop Node.js/`NodeInstaller`/`GitBashInstaller` entirely (dead weight / unnecessary on Linux).
- `ffmpeg` dependency: fetch a Linux build artifact, same bundling idea as Windows -- but see Phase
  4's Visual Mode finding: `ffmpeg` is only needed at all once a XcfaRenderer bridge exists, which it
  doesn't yet, so this is lower priority than it was when Visual Mode was assumed portable.
  `codex-app-server`: fetch a Linux build, same as Windows' own `CodexInstaller`-shaped step.
- `DefenderExclusion.cs`/`RestartManagerHelper.cs`: no Linux equivalent, drop entirely -- also
  means the Linux installer needs **no elevation at all**, a real simplification vs. Windows.
- `ShortcutManager.cs`'s COM-based `.lnk` creation -> `linux-shell/caroline.desktop` (already
  written and verified, see Phase 4) -- the installer's job is substituting `@INSTALL_DIR@` and
  copying it into `~/.local/share/applications/` and `~/.config/autostart/`.
- **`make dist-linux` done and verified (2026-10-06)**: assembles `linux-shell/` (fonts/icons
  included), `backend-py/` (app, `run_server.py`, `supervisor.py`, the same conditional camerlengo
  vendoring the Windows build does), and `wwwroot` copied as a real sibling -- no `dotnet publish`,
  since there's nothing to compile. `caroline_shell.py`'s own `_default_wwwroot()` checks that
  sibling location first now (falling back to the WPF project's copy only for an un-packaged dev-
  tree checkout). Verified end to end: copied the assembled tree to a clean directory on the test
  box, ran it with only `CAROLINE_APP_ROOT` set (no other env var overrides), and the full UI
  rendered correctly -- the first time this shell ran with every path resolved from packaging
  conventions instead of hand-set env vars.
- **AppImage wrapping validated end to end (2026-10-06)**, separately from the still-open
  `runtime/python`/`runtime/codex` provisioning gap below: built a real `.AppImage` (`appimagetool`,
  an `AppRun` script setting `CAROLINE_APP_ROOT` and exec'ing the system python3 against
  `linux-shell/caroline_shell.py`) from the `dist-linux` output plus a manually-assembled
  `runtime/python` venv, and ran it directly (simulating a double-click) on the test box -- it
  launched, resolved every path correctly, and rendered the splash screen. FUSE is available on the
  test box (`/dev/fuse`, `fusermount`/`fusermount3`); `--appimage-extract-and-run` also works as a
  fallback for environments without it. Not yet wired into the Makefile as a real target -- this was
  a manual proof that the mechanism itself works, blocked from being a committed target by the same
  `runtime/` provisioning gap (an AppImage with no backend runtime inside it would be genuinely
  broken, not just untested). Also surfaced and fixed a real shell bug while testing this: a missing
  bundled runtime used to fail with nothing visible at all (no window, no terminal output -- `log()`
  only writes to the log file); fixed to show a GTK error dialog naming the expected path.

**Gap found 2026-10-05, `runtime/python` half resolved same day.** `CarolineInstaller` itself never
installs anything -- it only downloads and extracts a pre-built `Caroline.zip`
(`DownloadsInfo.cs`/`Downloader.cs`) that already contains a working `runtime/python` (with
`claude_agent_sdk` in site-packages) and a working `runtime/codex` (with a compiled
`codex-app-server` binary). No script in this repo built that `runtime/` tree from a clean
checkout -- confirmed by the same search that found no `requirements.txt`/pinned-deps manifest
anywhere (see Phase 2's own note). It was assembled by hand at some point and had just been reused
since.

Both SDKs do publish for Linux (confirmed 2026-10-05): `claude-agent-sdk` ships glibc 2.17+ x86-64
and ARM64 wheels on PyPI, each bundling a prebuilt `claude` CLI; Codex's `app-server` officially
supports Linux (`codex app-server daemon bootstrap --remote-control`, same interface
`backend-py/app/engines/codex_rpc.py` already talks to). So this was never a Linux-specific
blocker -- Phase 5 just couldn't "do what Windows does," because what Windows does is undocumented
and manual.

`runtime/python` now has a real, repeatable build step: `backend-py/requirements-linux.txt` (a
pinned dependency list, captured by downloading a clean `python-build-standalone` CPython
3.12.15 and iteratively resolving every `ModuleNotFoundError` hit by `from app.main import PORT,
app` until it imported cleanly -- 41 packages, notably without the Windows-only automation extras
`pywin32`/`playwright`/`edge-tts`/`edge-playback`, which are tied to plugins already confirmed
Linux-incompatible in earlier phases and simply fail to import there) and
`linux-shell/provision_runtime.sh <install_root>`, which downloads that same
python-build-standalone release, installs the pinned requirements into it, and verifies with the
same import check. Verified end-to-end on the test box on top of a real `dist-linux` output: ran
the actual `supervisor.py` (not just the import) under the freshly provisioned `runtime/python`,
and it reached a fully healthy state -- `backend_started`, `GET /status` and `GET /api/status`
both 200, no traceback, normal engine startup log lines (`ensure_recurring_backup_already_seeded`,
`due_check_loop_starting`, etc.).

`runtime/codex` is provisioned by the same script, pinned to the same release as Windows' own
`CodexInstaller.cs` (`0.155.1`): the upstream `codex-app-server-package-x86_64-unknown-linux-musl.tar.gz`,
sha256-verified against the release's own `codex-package_SHA256SUMS`. Its layout (`bin/codex-app-server`
with sibling `bin/codex-code-mode-host`, `codex-path/rg`, `codex-resources/`) is exactly what
`supervisor.py` already expects for Linux. Deliberately NOT using `chatgpt.com/codex/install.sh`: it
edits shell profiles and symlinks into `$HOME`, which is wrong for a self-contained `runtime/` tree.
Unlike Windows, this is fetched from GitHub rather than our own dependency mirror -- the mirror has no
Linux archive uploaded yet, so this is an open follow-up, not a decided design.

Verified end-to-end on a clean `runtime/`: the script provisioned both trees from scratch, and the
supervisor started healthy against them.

**Full UI pass, 2026-10-05 (test box, seeded test data).** Screens captured: splash with the real
onboarding banners, main chat with a restored transcript, tab strip and mode menu, settings panel,
already-running and missing-runtime dialogs, login viewer, Monaco code viewer, image, video and
slideshow viewers. Fixed along the way:
- `websockets` and `httptools` were missing from the pinned list (chat stuck in a reconnect loop,
  uvicorn answering the WebSocket upgrade with 404). Both are now pinned at the Windows runtime's versions.
- Pictogram glyphs rendered as empty boxes. Replaced with drawn icons, Linux-only: static buttons via
  `ICON_CSS`, dynamic glyphs (speaker, file chips, document links) via `GLYPH_JS`. Windows UI untouched.
- Injected stylesheets used relative `/icons/` and `/fonts/` URLs, which WebKit resolves against
  about:blank. Now absolute.
- WebKit keys `localStorage` by program name, so the chat transcript location depended on the script
  filename. `GLib.set_prgname("caroline")` pins it.
- Image and video viewers loaded `file://` media from an about:blank page, which WebKit refuses. The
  page now uses the file's own directory as base URI.
- Login viewer labels were dark on dark. Now match the Windows login window colors.
- `dist-linux` now copies `SplashBanners` from the installer's banner set, as the Windows build does.

New runtime dependency: video playback needs the GStreamer plugin sets
(`gstreamer1.0-plugins-good`, `-bad`, `-libav`, `-ugly`) on the host.

## Maintenance-burden note (for future reference)

backend-py (the bulk of day-to-day feature work -- plugins, policies, memory) stays a single shared
codebase; Linux changes almost nothing there. The real, recurring cost concentrates in two places
with zero code reuse between platforms: the shell (WPF vs. Avalonia) and the native automation layer
(Win32 C# exes vs. Python/X11) -- any feature touching either now needs two implementations, and the
Makefile/CI build matrix doubles. Day-to-day backend work is unaffected; shell/automation work costs
roughly 1.5-2x once both platforms are live.

## Dev/debug environment note

WSL2 with WSLg covers most of the day-to-day loop (backend-py, Avalonia window rendering, Playwright
browser windows, and most X11 automation primitives via XWayland) without needing a separate Linux
box. Known gaps: **no tray icon testing at all** (WSLg has no panel/StatusNotifierItem host), and
global-hotkey/window-stacking behavior under XWayland is uncertain and needs verifying on a real X11
desktop session before relying on it. A real Linux machine/VM is still worth having for final
pre-release validation.

## Critical files (representative, not exhaustive)

- `backend-py/app/workspace_dir.py`, `supervisor.py`, `app/chat_session.py` (~line 2507),
  `app/plugins/files_plugin.py`, `app/local_stt.py` -- Phase 1.
- `backend-py/app/plugins/native_exe.py` and each of `{mouse,keyboard,inspect,chain,screenshot,
  window_mouse,window_keyboard,window_screenshot}_plugin.py` -- Phase 2.
- `backend-py/app/plugins/app_browser_plugin.py`, `app_browser_cdp.py` -- Phase 3.
- `Windows/Caroline/{MainWindow,SplashWindow,DocumentViewerWindow,VisualModeWindow}.xaml(.cs)`,
  `Tray/TrayIconManager.cs`, `Interop/GlobalHotkeyService.cs`, `Services/AutoStartService.cs` --
  Phase 4 (new Avalonia-based sibling project, Windows WPF project untouched).
- `Windows/CarolineInstaller/*` -- Phase 5 (new Linux bootstrapper project, same reasoning).
- `Makefile` -- Phase 5, add Linux publish targets alongside the existing Windows ones.

## Verification plan

- Phase 1: run `backend-py` standalone under WSL2 with `CAROLINE_WORKSPACE_DIR` pointed at a
  scratch dir, confirm `supervisor.py`'s HTTP control API works end to end with no Windows-only
  code path ever hit.
- Phase 2: a standalone script per plugin exercising each tool directly against a real X11 session
  (WSLg for most primitives; a real X11 desktop for anything flagged uncertain above).
- Phase 3: open a labeled Playwright window, confirm `app_browser_cdp.py`'s existing tools work
  against it unchanged; confirm `/fill_file_dialog`'s Playwright-native replacement actually
  uploads a file on a real site.
- Phase 4: manual visual verification of each ported window surface side by side with the Windows
  build; tray icon and global hotkey specifically need a real Linux desktop, not just WSL.
- Phase 5: a clean Linux VM/container with nothing pre-installed, run the AppImage end-to-end,
  confirm Caroline reaches a working chat state with no manual intervention.

No code changes without the user's own explicit go-ahead per phase -- each phase is its own
separate approval/implementation/verification cycle, not one single pass. Phase 1 is approved to
begin now; Phases 2-5 each get their own check-in before implementation starts.

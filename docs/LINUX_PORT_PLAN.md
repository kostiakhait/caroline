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

### Phase 5 -- installer/packaging

- `dotnet publish -r linux-x64 --self-contained` producing one binary, same model as Windows.
- AppImage-wrap that binary (`appimagetool`) for reliable double-click launch.
- Bundle a portable Python via `python-build-standalone`'s Linux builds (no system-Python
  dependency, matching the Windows embeddable-Python bundling philosophy).
- Drop Node.js/`NodeInstaller`/`GitBashInstaller` entirely (dead weight / unnecessary on Linux).
- `ffmpeg`/`codex-app-server` dependency installers: fetch Linux build artifacts.
- `DefenderExclusion.cs`/`RestartManagerHelper.cs`: no Linux equivalent, drop entirely -- also
  means the Linux installer needs **no elevation at all**, a real simplification vs. Windows.
- `ShortcutManager.cs`'s COM-based `.lnk` creation -> a plain `.desktop` file (simpler, no COM
  interop needed).
- `Makefile`: add a `linux-x64` publish path alongside the existing `win-x64` ones; replace the
  PowerShell-based SHA-256/date commands in the zip-packaging step with `sha256sum`/`date`.

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

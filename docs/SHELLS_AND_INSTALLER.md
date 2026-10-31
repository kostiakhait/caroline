# Shells, installer and Visual Mode

The backend is shell-agnostic. Two shells exist (Windows and Linux) and share one web UI; a third
form, the IntelliCat edition, is the Linux build with some code removed.

## Windows shell (`Windows/Caroline/`, WPF, .NET 8)

Published self-contained, single file (`dotnet publish -r win-x64 --self-contained
-p:PublishSingleFile=true`). It needs no .NET on the target machine; the *native helper* projects are
framework-dependent and do need the .NET 8 Desktop Runtime.

| Piece | Role |
| --- | --- |
| `App.xaml.cs`, `MainWindow.xaml.cs` | Window, tab strip (`ChatTab`, one WebView2 per tab, tab list persisted and synced to the backend), `PollSupervisorStatus`, web-message handling from the page, show/hide, exit |
| `Tray/TrayIconManager.cs`, `Interop/GlobalHotkeyService.cs` | Tray icon (incl. the update item) and the global hotkey (default Ctrl+Alt+C) |
| `Native/SupervisorClient.cs` | Starts `supervisor.py` with the **bundled** `runtime\python\pythonw.exe` (never a system Python, and fails loudly when the runtime is missing). Sets `CAROLINE_APP_ROOT`, `CAROLINE_PORT`, `CAROLINE_SUPERVISOR_PORT` |
| `Native/BackendProcess.cs`, `Native/SupervisorProcessGuard.cs`, `Native/StaleInstallCleanup.cs`, `Native/InstallState.cs` | Backend port constants, orphan protection, old install folder cleanup, which app folder is active |
| `Native/AppControlHost.cs`, `Native/VisualModelDownloader.cs` | Control channel to the page; downloading Visual Mode models |
| `DocumentViewerWindow`, `VisualModeWindow`, `SplashWindow`, `WizardWindow` | Secondary windows. The wizard hosts the backend's `/wizard` page |
| `Services/UpdateChecker.cs` | Polls every 30 min; on a newer `Caroline.zip` hash shows the update prompt and runs `CarolineInstaller.exe --silent-update`. The target design (one backend-decided rule, no modal) is in `UPDATE_EVERYWHERE_PLAN.md` |
| `Services/SettingsService.cs`, `Models/AppSettings.cs`, `Services/AutoStartService.cs`, `Services/Logger.cs`, `Services/WebViewOriginGuard.cs` | Window geometry/hotkey/open tabs, autostart, logging to `caroline.log`, WebView origin restriction |
| `wwwroot/` | The UI shared with Linux: `chat.html` + `chat.js`, `monaco_viewer.html` (code viewer; Monaco bundled under `vendor/monaco`), `office_editor.html` (OnlyOffice), `slideshow.html`, `visualmode.html` |

`Windows/Caroline.NativeHost/` hosts the native automation helpers (built framework-dependent).
`Windows/Caroline.Linux/` is the Linux counterpart of the native pieces.

Start-up verification (also in `BUILD.md`): `%LOCALAPPDATA%\Caroline\caroline.log` should show
`=== Caroline starting ===` followed by `backend listening on ws://127.0.0.1:48765`. A stack
trace right after `Process.Start() returned` means the backend crashed on launch and the window
sits on "Connecting..." forever.

## Linux shell (`linux-shell/`)

`caroline_shell.py` is a GTK window hosting the **same** chat page in WebKit2. The page talks to its
host through `window.chrome.webview.postMessage` (a WebView2 API), so a document-start shim maps that
call onto a WebKit script-message handler. The shell also owns the supervisor/backend lifecycle.

| File | Port of |
| --- | --- |
| `hotkeys.py` | `GlobalHotkeyService.cs` (XGrabKey on the root window; failure detected through the asynchronous error event) |
| `settings_service.py` | `SettingsService.cs` / `AppSettings.cs` |
| `update_checker.py` | `UpdateChecker.cs`; asks the backend `GET /api/update/status` and does the one thing only a shell can: replace the AppImage and restart |
| `viewer_window.py` | `DocumentViewerWindow` (image, video, code, slideshow) |
| `visual_bridge.py`, `visual_window.py` | Visual Mode: the C# renderer runs as a headless child process (`XcfaBridge`) over a JSON-lines pipe; a borderless transparent always-on-top window plays the `.webm` clips (VP9 with alpha) |
| `provision_runtime.sh`, `build_appimage.sh`, `AppRun`, `caroline.desktop` | Runtime provisioning (python-build-standalone, Node, codex) and AppImage assembly. `appimagetool` is a Linux ELF; these only run on a Linux host |

The runtime pins are in `backend-py/requirements-linux.txt` (Windows-only packages such as
`pywin32` and `edge-playback` are absent; plugins that need them fail to import and are skipped).
`LINUX_PORT_PLAN.md` has the phases and what was verified live. The host still needs
`python3-gi`, GTK3 and WebKitGTK; a fully self-contained AppImage is not attempted yet.

## IntelliCat edition

`make dist-intellicat` copies the Linux dist and removes everything that serves the Claude and
OpenAI sources: files listed in `tools/edition_cut_files.txt` and regions marked `edition:cut` in
the sources (a text pass by `tools/edition_build.py`; `edition_check.sh` and `edition_flakes.py`
check the result). The tree stays one tree; the default build is unaffected. See
`INTELLICAT_EDITION.md`.

## Installer (`Windows/CarolineInstaller/`)

A thin WPF bootstrapper, per-user, **no elevation**. Re-running it updates.

1. Takes a global mutex (`Global\CarolineInstallerRun`) so two installers never fight over the
   same folder; neutralises earlier copies.
2. Downloads `Caroline.zip` from `downloader.multi-portal.org` over HTTPS and verifies its SHA-256
   against the published `.sha256` (`Downloader.cs`, `HttpRetry.cs`).
3. Extracts into a **versioned app folder** under `%LocalAppData%\Caroline\` (the active folder is
   tracked in `state.json` by `InstallState.cs`, so an update does not delete the folder in use).
4. Provisions the isolated runtime under `%LocalAppData%\Caroline\runtime\`, each dependency
   pinned, sha256-verified and idempotent, never touching system PATH (`Dependencies/`):
   Python (embeddable), Node, a JRE and `signal-cli`, WebView2, ffmpeg, Git Bash, the Codex
   app-server, and the WhatsApp / Messenger sidecars. Playwright's Chromium ships in `dist/browsers`.
5. Downloads the Whisper model (`WhisperModelInstaller.cs`) and the Visual Mode models into
   `art\` (kept across updates), adds a Microsoft Defender exclusion (`DefenderExclusion.cs`),
   autostart and a desktop shortcut (`Autostart.cs`, `ShortcutManager.cs`), and launches the app.
6. Every failure shows an error code (`InstallerException`, `ErrorCodes`) and the log path.

`--silent-update` (used by the running app) shows no progress banner or success dialog, but a
failure is always shown. Logs: `%LOCALAPPDATA%\Temp\CarolineInstaller.log`; a good run ends with
`RunAsync completed normally`.

## Visual Mode (talking head)

Settings toggle plus a model per persona (`.xcfa` talking-head files, tens of GB, kept out of git,
downloaded at install time or bought as extra models through the wallet via
`visual_models_plugin`). `visual_mode.py` owns the on/off switch and which model backs the current
persona; rendering is native to the shell (`VisualModeManager.cs` in-process on Windows,
`XcfaRenderer` child process on Linux, source in `vendor/XcfaRenderer`).

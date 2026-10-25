# Caroline

A standalone desktop AI secretary: email, notes, browser and Windows
automation, messaging, phone-companion SMS, driven by natural-language
chat, without needing an IDE or a terminal open. Tray app, global hotkey,
WPF shell around a local chat UI. A tab can answer through the user's own
Claude account, the user's own ChatGPT/Codex account, or pay-as-you-go
through a SquirrelWisdom wallet -- same assistant, switchable per tab.

See `PROJECT.md` for the full, code-derived architecture writeup --
including the server-side **microagent** layer (reply review, a permission
guard that stops a turn until the user actually says yes, and the memory
agents behind long-term notes) backing several of these features.

Caroline is built to be **portable**: nothing at runtime references any
repo outside this one. Everything it needs ships inside its own install
directory, is provisioned by the installer into an isolated runtime
directory (see below), or lives in a per-user workspace directory created
on first run (`%LOCALAPPDATA%\Caroline\workspace\`).

## External requirements (target machine)

These are not bundled and must already be present wherever Caroline is
installed:

- **Windows 10/11, x64.** The bundled automation helpers are win32-x64 only.
- **.NET 8 Desktop Runtime** -- required by the native Windows-automation
  helper executables (`mouse.exe`, `keyboard.exe`, `screenshot.exe`, etc.).
  These are framework-dependent builds (`dotnet build`, not a self-contained
  publish), so the runtime must be present separately. The WPF shell itself,
  by contrast, is published self-contained
  (`dotnet publish --self-contained -p:PublishSingleFile=true`) and needs
  nothing extra.
- **An account to chat with**: a Claude subscription (Pro/Max) or Anthropic
  Console account, a ChatGPT/Codex account, or a SquirrelWisdom wallet to
  pay as you go -- any one is enough to start. Log in from inside Caroline
  (Settings), never by typing a key into chat.

Everything else the app needs on the native-automation/messaging side --
Node.js, Python, a JRE (for Signal), WebView2, ffmpeg, git-bash, Playwright's
Chromium, `codex-app-server`, the WhatsApp sidecar -- is provisioned by
`CarolineInstaller.exe` itself into `%LocalAppData%\Caroline\runtime\`
(`Windows/CarolineInstaller/Dependencies/*Installer.cs`, one pinned,
sha256-verified installer per dependency), never from system PATH and never
left for the target machine to already have.

## Architecture

- `Windows/Caroline/` -- WPF shell (C#): tray icon, global hotkey, WebView2
  hosting the chat UI, Settings screen. Spawns/supervises the backend
  sidecar process.
- `backend-py/` -- the live Python backend (FastAPI). `app/chat_session.py`
  is the per-tab core; `app/plugins/` is the tool roster, auto-wired to the
  model as MCP servers; `app/*_channel.py` own the always-running messenger
  connections. See `PROJECT.md`.
- `backend-py/skills-src/` -- the default skills, seeded into the workspace
  on every start. Every tool lives in `backend-py` (desktop automation:
  `app/plugins/_win32*.py` on Windows, `_x11_*.py`/`_linux_chain.py` on
  Linux); the old Node/TypeScript backend, its MCP servers and native helper
  exes were removed on 2026-10-24.
- Adding/removing MCP servers or skills after install is just the normal
  Claude Code mechanism (`claude mcp add/remove`, editing
  `%LOCALAPPDATA%\Caroline\workspace\Skills\`) -- exposed through Caroline's
  Settings screen, not a custom config format.

## Known limitations (v1, not silently solved)

- Only one default browser-automation profile is seeded (`caroline-browser`,
  port 9822).

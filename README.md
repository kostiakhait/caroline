# Caroline

A standalone desktop AI secretary: email, notes, browser and Windows
automation, driven by natural-language chat, without needing an IDE or a
terminal open. Tray app, global hotkey, WPF shell around a local chat UI,
backed by the Claude Agent SDK.

Caroline is built to be **portable**: nothing at runtime references any
repo outside this one. Everything it needs ships inside its own install
directory or lives in a per-user workspace directory created on first run
(`%LOCALAPPDATA%\Caroline\workspace\`). See
`backend/scripts/bundle-mcp-servers.mjs` for the one place that *does* read
from elsewhere in this repo (`backend/mcp-servers-src/*`) -- a dev-time
packaging step, not part of the shipped app.

## External requirements (target machine)

These are not bundled and must already be present wherever Caroline is
installed -- relevant for the installer:

- **Windows 10/11, x64.** The bundled automation helpers are win32-x64 only.
- **Node.js** (v18+; developed against v22) -- runs the backend sidecar and
  the MCP server wrappers it spawns. Not bundled; the installer needs to
  either require it as a prerequisite or bundle a Node runtime.
- **.NET 8 Desktop Runtime** -- required by the native Windows-automation
  helper executables (`mouse.exe`, `keyboard.exe`, `screenshot.exe`, etc.,
  bundled under `backend/mcp-servers/*/dist/`). These are framework-dependent
  builds (`dotnet build`, not a self-contained publish), so the runtime must
  be present separately. The WPF shell itself, by contrast, is published
  self-contained (`dotnet publish --self-contained -p:PublishSingleFile=true`)
  and needs nothing extra.
- **WebView2 Runtime** -- hosts the chat UI inside the WPF shell. Usually
  preinstalled on Windows 11; may need installing on Windows 10.
- **A Claude subscription** (Pro/Max) or an Anthropic Console account. No API
  key to configure -- log in from inside Caroline (tray menu -> "Log in to
  Claude", runs the bundled CLI's own `claude auth login` OAuth flow). The
  Node backend's Claude Agent SDK dependency (`@anthropic-ai/claude-agent-sdk`)
  pulls in a fully self-contained, platform-specific `claude.exe`
  (`node_modules/@anthropic-ai/claude-agent-sdk-win32-x64/claude.exe`, ~200MB)
  via plain `npm install` -- no separate Claude Code install needed.

## Architecture

- `Windows/Caroline/` -- WPF shell (C#): tray icon, global hotkey, WebView2
  hosting the chat UI, Settings screen. Spawns/kills the backend sidecar.
- `backend/` -- Node/TypeScript sidecar. Exposes a loopback WebSocket
  (`ws://127.0.0.1:48765` by default) that the chat UI talks to. Wraps the
  Claude Agent SDK's streaming `query()`, with a watchdog that auto-restarts
  a hung or crashed session (replaying the unanswered message) so the app
  never needs a manual restart.
- `backend/mcp-servers/` -- default MCP servers, produced by
  `scripts/bundle-mcp-servers.mjs` from this repo's own
  `backend/mcp-servers-src/*` sources (each esbuild-bundled into a single
  `.mjs` file, native helpers copied alongside as-is -- see that script's
  own header comment). Registered once, on first run, at **user scope**
  (`claude mcp add --scope user`) under `caroline-`-prefixed names (e.g.
  `caroline-time`, `caroline-windows-mouse`) so they can never collide with
  or be shadowed by any other MCP server config already on the machine.
- Adding/removing MCP servers or skills after install is just the normal
  Claude Code mechanism (`claude mcp add/remove`, editing
  `%LOCALAPPDATA%\Caroline\workspace\Skills\`) -- exposed through Caroline's
  Settings screen, not a custom config format.

## Known limitations (v1, not silently solved)

- Only one default browser-automation profile is seeded (`caroline-browser`,
  port 9822).
- Node.js itself is an external prerequisite, not bundled -- the installer
  needs to either require it or ship a Node runtime.

# Configuration

Caroline has almost no config file. Runtime locations are handed to the backend by the shell as
`CAROLINE_*` environment variables; user settings live in the workspace and in Settings.

## Environment variables

Set by the shell / supervisor on every spawn (do not set these by hand on an installed system):

| Variable | Meaning |
| --- | --- |
| `CAROLINE_APP_ROOT` | Root above the active `app-<hash>` folder (the shell passes the folder above its own base directory) |
| `CAROLINE_PORT` / `CAROLINE_SUPERVISOR_PORT` | Backend port (default 48765) / supervisor port (48766) |
| `CAROLINE_NODE_PATH`, `CAROLINE_PYTHON_PATH`, `CAROLINE_JAVA_HOME`, `CAROLINE_FFMPEG_PATH`, `CAROLINE_CODEX_PATH`, `CAROLINE_SIGNAL_CLI_BAT` | Locations of the isolated runtime tools under `%LocalAppData%\Caroline\runtime\` |
| `CAROLINE_NATIVEHOST_EXE_PATH` | The native helper host, a sibling of `Caroline.exe` in the app folder |
| `CAROLINE_MODELS_DIR`, `CAROLINE_WHISPER_MODEL_PATH`, `CAROLINE_BROWSERS_DIR` | Visual Mode models (outside the app folder, survive updates), local Whisper model, bundled Chromium |
| `CAROLINE_WHATSAPP_SIDECAR_DIR`, `CAROLINE_MESSENGER_SIDECAR_DIR` | Sidecar install locations |

Operator / developer switches:

| Variable | Effect |
| --- | --- |
| `CAROLINE_WORKSPACE_DIR` | Workspace location (default `%LOCALAPPDATA%\Caroline\workspace`, resolved in `workspace_dir.py`) |
| `CAROLINE_REMOTE_PORT`, `CAROLINE_REMOTE_ACCESS` | Second listener on 0.0.0.0 (default port 48768) and its gate |
| `CAROLINE_FRONTEND_ORIGIN` | Allowed web origin for the page (default `https://caroline.local`) |
| `CAROLINE_SKILL_DIRS` | Extra directories scanned for skills |
| `CAROLINE_FLAT_TOOLS=1` | Register every tool directly instead of the four group tools |
| `CAROLINE_NO_LADDER=1` | Disable the OpenAI/Codex effort ladder (the Claude ladder is disabled by a manual model override) |
| `CAROLINE_NO_DELIVERY_CHECK=1` | Skip the end-of-turn delivery report |
| `CAROLINE_CODEX_HOME`, `CAROLINE_CODEX_CONFIG_OVERRIDES` | Dedicated Codex state directory; extra config overrides for the app-server |
| `CAROLINE_CAMERLENGO_PATH` | Where to find the server helper modules (`AI.py`, `Microagent.py`) in a non-standard dev layout |
| `CAROLINE_SW_USER`, `CAROLINE_SW_SESSION`, `CAROLINE_SW_KEY` | A host-provided SquirrelWisdom session (Xopek desktops); in that mode no password exists (`external_session.py`) |
| `CAROLINE_SETTINGS_FOLDER`, `CAROLINE_SETTINGS_VAULT_FOLDER` | Notes folders of the settings mirror (defaults `Caroline:Settings`, `Caroline:Vault`) |
| `CAROLINE_UPDATE_URL`, `CAROLINE_UPDATE_PLATFORM`, `CAROLINE_UPDATE_DIR`, `CAROLINE_UPDATE_DISABLE`, `CAROLINE_RELEASE_PUBLIC_KEY`, `CAROLINE_RUNTIME` | Update client: manifest base URL, platform id, download directory, kill switch, key override, platform runtime number |
| `BUS_URL`, `AGENT_UID` | Turn this process into an Akinakes bus agent |

## Files

| Where | What |
| --- | --- |
| `%LOCALAPPDATA%\Caroline\caroline.log` | Shell + backend log (JSON lines from the backend) |
| `%LOCALAPPDATA%\Temp\CarolineInstaller.log` | Installer log |
| `%LOCALAPPDATA%\Caroline\workspace\` | All persisted state, see [MEMORY_AND_STATE.md](MEMORY_AND_STATE.md) |
| `%LOCALAPPDATA%\Caroline\runtime\` | Isolated Node, Python, JRE, signal-cli, ffmpeg, Git, Codex |
| `%LOCALAPPDATA%\Caroline\art\` | Visual Mode and Whisper models (kept across updates) |
| `~/.mcp-notes/credentials.json` | SquirrelWisdom credentials shared by every SW-backed tool on the machine |
| `~/.claude/` | The Claude CLI's own state and transcripts |
| `~/.codex` or `CAROLINE_CODEX_HOME` | Codex state |
| `workspace/subscription.json` | Pasted Anthropic API key and source choice |

## Accounts and secrets

* Log in from inside Caroline (Settings or the wizard), never by typing a key into the chat. The
  native login form posts straight to the backend over a control op (`login_submit`); credentials
  never pass through the model.
* Messenger credentials and session state are encrypted at rest with DPAPI
  (`secret_store.py`, scoped to the Windows user). Other local state, including the SquirrelWisdom
  credentials file, is plain JSON; broader secrets a user shares in conversation go to the Notes
  `vault` folder instead of local files.
* SquirrelWisdom service keys live in `backend-py/app/sw_service_keys_local.py` (**gitignored**,
  template in `sw_service_keys_example.py`). They were once committed and have been rotated; never
  commit them and never paste them into docs or logs.
* The release-signing seed lives outside every repository (`tools/release.py keygen`).
* `deploy.mk` and the uploader's `deploy_token.txt` are maintainer-only and gitignored.

## Settings screen

Persona editor, window (always on top), voice (auto-send), speech recognition (local STT on/off),
Visual Mode, models (Claude / OpenAI pickers and the manual model override), Claude account,
Account & Billing (resolved source, own API key, SquirrelWisdom login / balance / top-up), OpenAI
account, Ratatosk, MCP servers (the standard `claude mcp add/remove` mechanism, not a custom
format). Backed by the control ops listed in [BACKEND.md](BACKEND.md). Settings files are mirrored
to Notes (`settings_mirror.py`).

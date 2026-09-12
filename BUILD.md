# Building and deploying Caroline

The build pipeline is a real `Makefile` (repo root) with file-based
dependencies -- not `build.bat`/`build_installer.bat` (those are thin
wrappers that just call into it; kept around for convenience, not an
independent path). **Always use `make`, never hand-run its steps
individually** -- see "Why this matters" below.

`make deploy`/`make deploy-models` (uploading a release to the maintainer's
own server) are **not part of a fresh clone** -- they live in `deploy.mk`,
which is gitignored and only exists on the maintainer's own machine (see
that file's own header comment for why). Everything below through "Common
invocations" applies to any clone; the "Deploy" sections after that are
maintainer-only reference.

## Prerequisites and where to find them

- **.NET 8 SDK** -- for `dotnet publish`.
- **Node.js + npm** -- for the backend build.
- **Python** -- used only for zip packing (`zipfile` module); PowerShell's
  `Compress-Archive` hangs on this machine packing `dist/` (see the
  Makefile's own comment on the zip recipe).
- **Git Bash** -- the Makefile's `SHELL` is pinned to
  `C:/Program Files/Git/bin/bash.exe`.
- **GNU Make itself.** On this machine it is installed at:
  ```
  C:\Program Files (x86)\GnuWin32\bin\make.exe
  ```
  It is **not** on PATH by default in every shell (confirmed: absent from a
  plain Git-Bash/Claude-Code-tool PATH). If `make` isn't found, add it to
  PATH for that session rather than assuming it isn't installed:
  ```bash
  export PATH="/c/Program Files (x86)/GnuWin32/bin:$PATH"
  ```
  (PowerShell: `$env:PATH = "C:\Program Files (x86)\GnuWin32\bin;$env:PATH"`)

Maintainer-only, for `make deploy`/`make deploy-models` (via `deploy.mk`):

- **Go** -- to build `uploader.exe` (the deploy client), only if it doesn't
  already exist or its source changed. Lives outside this repo entirely
  (see `deploy.mk`'s `UPLOADER_DIR`).
- **`<UPLOADER_DIR>/deploy_token.txt`** -- the bearer token printed when the
  `caroline-uploader` systemd service was first started on the remote host
  (`downloader.multi-portal.org`). Not checked in anywhere. If missing,
  `make deploy` fails fast with a clear error telling you the same thing.

## Common invocations

```bash
make build          # app itself only, no packaging
make installer       # + Caroline.zip/.sha256/.version + CarolineInstaller.exe
make deploy           # + uploads the four release files (cheap/no-op if
                       #   nothing upstream actually changed)
make deploy-models   # uploads art/models/*.xcfa (rarely needed --
                       # tens of GB, separate from the routine app deploy)
make clean            # removes dist/ and dist_installer/ (forces a full
                       # rebuild next time; normally unnecessary)
```

`make deploy` is always safe to run repeatedly -- Make's own dependency
tracking (real file mtimes, not phony "always rebuild" targets) means it
only redoes the steps whose actual inputs changed since last time.

## Why this matters (don't hand-run the steps)

The Makefile's own build recipe copies the backend's `node_modules`,
`dist/`, `mcp-servers/`, and `skills-src/` into the packaged app in
**parallel background jobs**, then `wait`s for them:

```make
cp -r "$(BACKEND_DIR)/dist" "$(OUT)/backend/dist" & \
cp -r "$(BACKEND_DIR)/node_modules" "$(OUT)/backend/node_modules" & \
cp -r "$(BACKEND_DIR)/mcp-servers" "$(OUT)/backend/mcp-servers" & \
cp -r "$(BACKEND_DIR)/skills-src" "$(OUT)/backend/skills-src" & \
wait
```

Confirmed live (2026-09-04): running this by hand through a tool that can
silently time out and "move a command to the background" produced a
`dist/backend/node_modules` that looked like it copied successfully (the
overall command reported exit code 0) but was actually **missing packages**
(`ws`, at minimum) -- because the copy of that one directory got interrupted
partway, and a bare `wait` only reflects the last-checked job, not all four.
The resulting zip got built, hashed, and deployed anyway, and the installed
app's backend then crashed on every single startup with
`ERR_MODULE_NOT_FOUND: Cannot find package 'ws'`, hanging the whole app on
"Connecting..." forever with no error surfaced anywhere obvious.

Running the real `make deploy` doesn't fix that specific race by itself, but
it does mean you're running the pipeline the way it's actually tested and
maintained, instead of re-typing (and potentially getting subtly wrong) its
steps from memory. **If `make` seems unavailable, find it (see above) or
ask -- don't hand-replicate the recipe.**

## Where things end up

- `dist/` -- unpacked app (WPF shell + `backend/` sidecar), portable, no
  reference back to this repo at runtime.
- `dist_installer/` -- `Caroline.zip` (+ `.sha256`/`.version`) and
  `CarolineInstaller.exe` (+ `.sha256`), the four/five files `make deploy`
  uploads.
- Deployed to `downloader.multi-portal.org:41981` ->
  `/var/www/html/apps/caroline/`, served at
  `https://downloader.multi-portal.org/apps/caroline/...`.
- Models (`make deploy-models`) go to the `models/` subpath there instead.

## Verifying a deploy actually worked

Don't just trust "Deploy complete" / exit code 0. After installing:

- Check `%LOCALAPPDATA%\Temp\CarolineInstaller.log` for a clean run ending in
  `RunAsync completed normally`, with no `ExtractWithRetryAsync` retries.
- Check `%LOCALAPPDATA%\Caroline\caroline.log`'s tail for
  `[backend] [caroline] backend listening on ws://127.0.0.1:48765` shortly
  after `=== Caroline starting ===` -- if instead you see a Node stack trace
  (`ERR_MODULE_NOT_FOUND`, `SyntaxError`, etc.) right after
  `[BackendProcess] Process.Start() returned`, the backend crashed on launch
  and the app will sit on "Connecting..." forever, exactly like the incident
  above.

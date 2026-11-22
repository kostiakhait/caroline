# Build, release and update

`BUILD.md` in the repository root is the short operational note; this document explains the whole
chain. **Always build through `make`; never hand-run its steps.** A hand-copied step once produced
a package with missing files that was zipped, deployed and then crashed on every start.

## Prerequisites (build machine)

.NET 8 SDK, Node.js + npm, Python (used only for zip packing, because PowerShell's
`Compress-Archive` hung on `dist/`), Git Bash (the Makefile pins `SHELL` to
`C:/Program Files/Git/bin/bash.exe`) and GNU Make 3.81-compatible syntax (GnuWin32 Make; add
`C:\Program Files (x86)\GnuWin32\bin` to PATH when it is missing). The Makefile also pulls the
server helper stubs from a sibling `reforce` clone (`CAMERLENGO_REPO`, default `../reforce`) via
`git show caroline:<path>`, so that branch must be reachable.

## Targets

| Target | Result |
| --- | --- |
| `make build` | `dist/`: the portable app (WPF shell, `backend-py`, sidecars, Playwright Chromium) |
| `make installer` | `dist_installer/`: `Caroline.zip`, `.sha256`, `.version`, `CarolineInstaller.exe`, `.sha256` |
| `make dist-linux` | `dist-linux/`: Linux tree (GTK shell, backend, sidecars). Runtime provisioning and the AppImage are Linux-host steps |
| `make dist-intellicat` | `dist-intellicat/`: the Linux dist with the Claude/OpenAI code cut out |
| `make appimage-linux` | Prints the two commands to run on a Linux host (`provision_runtime.sh`, `build_appimage.sh`) |
| `make clean` | Removes `dist/`, `dist_installer/`, `dist-linux/` |
| `make deploy`, `make deploy-models` | Only when the gitignored `deploy.mk` exists (maintainer's machine); upload to the release server |

### How `make build` is structured

Independent stamp targets, each depending only on the inputs it reads, so a Python-only change skips
both `dotnet publish` runs: `.dotnet.stamp` (Caroline + NativeHost + XcfaRenderer, published in
parallel), `backend-py/camerlengo/.stamp` (vendored helper stubs), `.backend-py.stamp` (copies
`backend-py/app` and `skills-src`), `.whatsapp-sidecar.stamp`, `.messenger-sidecar.stamp`,
`.browsers.stamp` (Playwright's Chromium, from the pinned browsers JSON). `dist/Caroline.exe` is the
final target. `-p:IncludeNativeLibrariesForSelfExtract=true` is deliberately **not** used for the
shell: it silently drops `wwwroot/assets/**` from the publish.

`Caroline.zip` is built with Python's `zipfile`; its `.version` is a `yyyyMMddHHmm` timestamp unless
you pass `VERSION=...`.

## Deploy layout

`downloader.multi-portal.org` → `/var/www/html/apps/caroline/` (Windows installer files),
`models/` subpath for Visual Mode models, `deps/` for the installer's dependency mirror
(`AppPaths.DependencyMirrorBaseUrl`). After a deploy verify the installer log and `caroline.log`
as described in [SHELLS_AND_INSTALLER.md](SHELLS_AND_INSTALLER.md).

## One signed release for every platform

Design: `UPDATE_EVERYWHERE_PLAN.md` (status per stage is in that document; Windows still uses the
older hash-compare path described there).

* **Manifest.** One `release.json` per release plus `release.json.sig`, a base64 Ed25519 signature
  over the exact bytes. Contents: `version`, `commit`, `date`, and `artifacts` per platform
  (`windows`, `linux-x86_64`, `beroun`, each with `file`, `sha256`, `size`, optional `runtime`
  number). The public half of the key is built into `app/release_manifest.py`
  (`RELEASE_PUBLIC_KEY`; override `CAROLINE_RELEASE_PUBLIC_KEY`). Nothing from the manifest is trusted
  before the signature verifies.
* **Release tool** `tools/release.py`:
  * `keygen KEYFILE`: make the key once, keep it outside every repository; prints the public key.
  * `build --version X.Y.Z --key KEYFILE --out DIR --artifact PLATFORM=PATH[,runtime=N]`: writes the
    manifest and signature. The artifacts are *not* copied: upload them together with the two
    files, all or none.
  * `overlay DIST OUT.zip`: the code-layer artifact for the `xopek` and `beroun` platforms.
  * `verify DIR`: check an uploaded or downloaded folder.
* **Client** `app/update_service.py`: reads `<base>/release.json[.sig]` (`CAROLINE_UPDATE_URL`),
  downloads and verifies this platform's artifact in the background, and reports a `status`:
  `up_to_date | available | downloading | ready | needs_runtime | failed | idle`. It stops at "a
  verified file at <path>"; installing belongs to the platform. The version is
  `backend-py/VERSION` (currently `1.0.0`). `CAROLINE_UPDATE_PLATFORM`, `CAROLINE_UPDATE_DIR`
  and `CAROLINE_UPDATE_DISABLE` tune or switch it off.
* **The rule** (`app/update_activity.py`, `GET /api/update/activity`): if Caroline is **inactive**
  the update installs by itself; if she is **in use** the owner is asked quietly (no modal) and it
  still installs at the first inactive moment. *In use* = a turn is running, continuous voice
  dialogue is on, a task / microagent / skill is running, or the owner touched a Caroline window in
  the last 10 minutes. All shells ask this one endpoint.
* **Overlay platforms** (`app/overlay_install.py`, `app/update_cli.py`): Xopek desktops and OS
  Beroun take a zip of only the *owned* paths (backend code, `wwwroot`, ...) applied over an
  installed dist, with no change to the runtime. `python -m app.update_cli apply [--root DIST]`
  runs before the shell starts and never fails the start (offline, no release or a bad file just
  leaves things as they are).

* **`make deploy` is a release (2026-10-30).** In order: `release-version` (`tools/release.py next-version --write`: `backend-py/VERSION` := the
  published version with the patch raised, or the owner's own higher VERSION; it refuses to guess when the published manifest cannot be read), the build,
  `deploy-legacy` (the old Windows files `Caroline.zip`, `CarolineInstaller.exe`, which installed Windows copies still read), then `release-publish`: the
  versioned Windows installer and payload, the `xopek` overlay (with `sw_service_keys_local.py`) and the `beroun` overlay (without it) are uploaded, and
  `release.json` with its signature goes up last. The key is `RELEASE_KEY` (default `~/.caroline/release-signing.key`). `make release-stage` does everything
  up to and including `release.py verify` in `dist_release/` and uploads nothing. The AppImage is built in WSL by `make appimage-wsl OUT=path` (`linux-shell/build_appimage_wsl.sh`): only the code is copied each time, the runtime
  (Python with its packages, Chromium, codex, node, signal-cli, the sidecars' node_modules) is provisioned in `~/.intellicat/lin-build/appimage/root` once and again only
  when `requirements-linux.txt`, `provision_runtime.sh` or a sidecar `package.json` changes (first build about 8 minutes, later ones about 2). `APPIMAGE=path` takes an
  AppImage made elsewhere for that same version; `NO_APPIMAGE=1` leaves `linux-x86_64` out of the release. `VERSION` is changed in the working tree, not committed.

## Versioning notes

* The app's own version is `backend-py/VERSION`; the Windows installer still compares the zip hash.
* The signing key must never enter a repository, and `sw_service_keys_local.py` / `deploy.mk` /
  `deploy_token.txt` are gitignored by design.

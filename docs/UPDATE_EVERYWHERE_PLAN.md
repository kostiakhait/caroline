# One release of Caroline, updated everywhere

**Aim (owner, 2026-10-30):** when Caroline is updated, the new version reaches **every** place she runs: Windows desktops, Linux, the Xopek desktops and the devices with
OS Beroun (IntelliCat, the local and hosted simulators, OS Beroun on Android). **Status: a plan, nothing built.**

## The rule (owner, 2026-10-30)

The same on every platform:

* **Caroline is inactive → the update installs by itself.** Nobody is asked.
* **Caroline is in use → the owner is asked**, without interrupting the work: no modal window, a quiet notice and an "Update to X" item. The update is downloaded and checked
  in the background meanwhile.
* **When she becomes inactive, a waiting update installs by itself**, whether or not the owner answered. "Later" therefore never means "never".

**In use** means any of: a turn of a conversation is running (chat or voice), the continuous voice dialogue is on, a task, a microagent or an agent's skill is running, or the owner
touched one of her windows in the last 10 minutes. **Inactive** is everything else, including "not loaded".

This decision is made in one place, the shared backend (`backend-py`), for every shell: `GET /api/update/activity` → `{"busy": bool, "reasons": [...], "idle_s": n}`. The
shells (Windows, Linux, Beroun) only ask it; none of them invents its own rule.

## Today (checked 2026-10-30)

| Where | How she is updated now |
| --- | --- |
| **Windows** | `Windows/Caroline/Services/UpdateChecker.cs`: every 30 min compares the installed `Caroline.zip`'s sha256 with `downloader.multi-portal.org/apps/caroline/Caroline.zip.sha256`; on a difference, a **modal MessageBox** "Restart now to update?" once per launch and a tray item; the update runs `CarolineInstaller.exe --silent-update`. Works. The MessageBox breaks the rule |
| **Linux** | `linux-shell/update_checker.py`: the AppImage replaces itself (sha256 checked, `os.replace`, restart). Only from a real AppImage; written, **never tried live**, and nothing is published at its URLs |
| **Xopek desktops** | Caroline is unpacked in the image (`/opt/caroline`), the updater is off; a new version comes only with a rebuilt image (`--pull=always` at a desktop's start). The owner chose an update inside the desktop on 2026-10-27 (`Xopek_Deskops/docs/CAROLINE_SW_SESSION_PLAN.md`, O8, variant (c)); not built |
| **OS Beroun** | Caroline (`dist-intellicat`: `backend-py`, `wwwroot`, the sidecars) is part of the firmware, from the pinned `Simulator/caroline` submodule; she changes only with a new firmware or a rebuilt hosted image. Her talking head (`ic-face-view`) is IntelliCat's own native program |
| **OS Beroun on Android** | Planned inside the APK (`IntelliCat/Simulator/docs/android-plan.md`) |

## 1. One release, one manifest

* A release of Caroline has **one version** for all platforms (`x.y.z`, plus the commit). One script (`tools/release.sh`) builds every artifact from the same commit and publishes
  them together, or none of them.
* Next to the artifacts, on `downloader.multi-portal.org/apps/caroline/`, one **release manifest** `release.json`:

```json
{
  "version": "1.42.0",
  "commit": "a7b052e",
  "date": "2026-10-30",
  "artifacts": {
    "windows":      {"file": "CarolineInstaller-1.42.0.exe", "sha256": "...", "size": 216000000},
    "linux-x86_64": {"file": "Caroline-1.42.0-x86_64.AppImage", "sha256": "...", "size": 0},
    "beroun":       {"file": "caroline-beroun-1.42.0.zip", "sha256": "...", "size": 0, "runtime": 3}
  }
}
```

* **Signed:** `release.json.sig`, Ed25519 over the file, with a key of its own (not the store's key of OS Beroun); the public half is built into every platform. An unsigned or
  wrongly signed manifest is ignored; an artifact whose sha256 differs is never installed.
* **Never a downgrade:** a platform installs only a version higher than its own.
* **`runtime`**: what a platform must already have that the update does not carry (section 3). A platform whose runtime is older does not install the release and waits for its own update.
* **The transition:** the old files (`Caroline.zip.sha256`, `.version`, `CarolineInstaller.exe`) are published too until the installed Windows builds have the new updater.

## 2. Each platform

Every platform does the same three things: look at `release.json` (a minute after the start, then every 30 minutes, a flat retry on failure, never a growing one), download and check in
the background, then **install by the rule**: at once if Caroline is inactive, otherwise the notice and the install at the first inactive moment.

| Where | Inactive → installs by itself | In use → asks |
| --- | --- | --- |
| **Windows** | **at the start, before her window shows** (she was not loaded: the update is the first thing she does); and while running, when the activity says idle: the silent installer, then she is started again in the same place | the tray item "Update to X" and a line in every tab's status bar; **the MessageBox goes** |
| **Linux (AppImage)** | the same, with the AppImage replacing itself | the same, tray and status bar |
| **Xopek desktops** | at the **desktop's start, before Caroline starts**, inside the desktop (variant (c) of O8: the update lives in the desktop's own layer, not on the persistent volume). Each release also rebuilds the image, so new desktops start with it anyway | the same as Linux, inside the desktop |
| **OS Beroun** (device, simulators, Android) | `shelld` installs her while her views are closed, the voice dialogue is off and nothing of hers is running (the device's sleep is the best moment), and restarts her backend; at the start, before her backend starts | a notification in the status bar of OS Beroun; A on it installs |

### OS Beroun in detail

* **What is updated:** the portable part only, `dist-intellicat` (`backend-py`, `wwwroot`, the sidecars' JS): the artifact `beroun`. Caroline is not an app (`IntelliCat/Simulator/docs/app-format.md`)
  and never comes through the app store; this is a system part of OS Beroun updated by `shelld`.
* **Where:** a writable folder of the owner's data (`~/.intellicat/caroline/<version>/`), next to the firmware's read-only copy. `shelld` starts the newest one that verifies; **if the new
  one fails to start, the previous one (in the end, the firmware's) is put back**, as the store does with apps.
* **The firmware's copy stays** as the fallback; a new firmware with a newer Caroline makes the downloaded older ones obsolete (they are deleted).
* **The hosted simulator:** a visitor's machine does the same at its start; the release also rebuilds the hosted image (`hosted/build-image.sh`), so new machines start with it.
* **Android:** the same code of OS Beroun, so the same update, **without a new APK**. Only what the APK carries natively (section 3) needs a new APK.
* Caroline gets the update API too (`beroun_*` tools: "is there an update", "update now"), as she gets every API of OS Beroun.

## 3. What an update cannot carry

The native parts come with their platform, not with Caroline's update: on OS Beroun the talking head (`ic-face-view`), the STT and face models, Node.js, `signal-cli`; on Android the
same inside the APK; on Xopek, the image. Each platform states its **runtime** number; a release that needs a newer runtime says so in `release.json` and waits there until the
firmware, the APK or the image catches up. Windows and Linux carry everything in their artifact and have no such limit.

## 4. Stages

1. **Done in the code (2026-10-30), no release published yet.** `backend-py/app/release_manifest.py` (the shared module every platform's updater uses: pure-Python Ed25519 checked against the RFC 8032 vector,
   `verify_manifest`, the shape rules, `is_newer` so a version never goes down, `artifact_for` with the `runtime` rule, `check_file` for the sha256 and the size) and `tools/release.py`
   (`keygen`, `build --artifact PLATFORM=PATH[,runtime=N]`, `verify`); `backend-py/test_release_manifest.py`, 8 tests, all pass, including the tool end to end. The tool is Python, not `release.sh`, so the
   maintainer's Windows machine runs it too. **The release key** was made on 2026-10-30 at `~/.caroline/release-signing.key` on the maintainer's machine (outside every repository; **to be backed up**, it
   cannot be remade: every installed Caroline will hold its public half), and the public half is `RELEASE_PUBLIC_KEY` in `release_manifest.py`. **Left:** building every artifact from one commit
   (the Windows installer and the Beroun zip here, the AppImage on Linux), the first real `release.json` and its upload to `downloader.multi-portal.org/apps/caroline/` next to the old files
   (they stay until the installed Windows builds have the new updater, stage 3). The upload needs the maintainer's uploader token; it waits for stage 3 so that a release has something that reads it.
2. **Done (2026-10-30).** `backend-py/app/update_activity.py` and `GET /api/update/activity` → `{ok, busy, reasons, idle_s}` (loopback only, like the rest of the backend). Reasons: `turn:<tab>` (a turn is running in any
   session, headless and Agent Bus ones too), `task:<tab>:<n>` (a background task or agent still running, from `agent_registry`), `dialogue` (the continuous-dialogue switch is on **and** the owner was active in the last 30
   minutes: the switch alone can stay on for weeks, so it does not block for ever), `user` (the owner sent something in the last 10 minutes; headless sessions have no owner). No session at all, or all ended, is inactive.
   An unreadable task file does not hold an update back. `idle_s` is the owner's idle time (null without a session). `test_update_activity.py`, 10 tests including the endpoint; with the release tests 24 pass.
   Not counted: a window that is open and touched but nothing sent (the backend does not see it); the shells of stages 3-6 may add that on their side.
3. **Done in the code (2026-10-30), compiled, not tried on a screen.** Found on the way: Caroline had **no version number** (the Windows build is a timestamp plus the sha256 of `Caroline.zip`), so "never a
   downgrade" had nothing to compare. Now **`backend-py/VERSION`** (`1.0.0`) is the one source: every platform ships backend-py, so every platform knows its version; the Windows and Linux recipes of the
   `Makefile` copy it, `tools/release.py build` takes it as the default `--version`. A release is: raise `VERSION`, commit, build every artifact from that commit.
   **The shared client moved into the backend**, not into each shell: `backend-py/app/update_service.py` (look at `release.json` + `.sig` 10 s after the start and every 30 min, flat 5 min retry after a failure,
   a 404 means "no release yet" and is quiet; download to a `.part`, check size and sha256 against the **signed** manifest, only then `ready`; a verified file is not downloaded twice; leftovers of older
   versions are removed; a release needing a newer `runtime` waits as `needs_runtime`). Endpoints: `GET /api/update/status` (state + `busy` + `action`: `install` | `ask` | `none`), `POST /api/update/check`,
   `POST /api/update/confirm` (the owner's yes), `POST /api/update/installing` (the platform started the install). The rule is `action`: inactive → `install`; in use → `ask` until the yes; a ready update
   therefore installs by itself at the first inactive moment. `CAROLINE_UPDATE_URL`, `_DIR`, `_PLATFORM`, `_DISABLE` and `CAROLINE_RUNTIME` are the knobs. A freshly started process is inactive (the
   activity now counts the owner's last **real turn**, `last_real_user_turn_at`, not the session's creation), so a waiting update is the first thing a started Caroline does.
   Tests: `test_update_service.py`, 10 tests against a **real local HTTP server** serving a signed release (newer / same / older, a wrong key, a tampered file, `needs_runtime`, another platform, no second
   download, the rule, leftovers); with the other update tests 29 pass.
   **Windows:** `Windows/Caroline/Services/UpdateChecker.cs` is now thin: it asks `/api/update/status` every 30 s and does the one thing only Windows can: `install` → start the installer (`--silent-update`
   when nobody asked, visible when the owner chose "Update to X" in the tray) and exit; `ask` → the tray item and a line in every tab's status bar, **no dialog**; the tray click is the yes
   (`UpdateNowAsync` → `/api/update/confirm`). The MessageBox is gone, so is `InstallerFetcher.cs` (the backend downloads and verifies now). `dotnet build` of `Caroline.csproj`: 0 errors.
   **Left for Windows:** (a) `CarolineInstaller` still takes `Caroline.zip` and its hash from the old unsigned `Caroline.zip.sha256` / `.version`; it should read the zip's hash from the signed manifest (add a
   `windows-zip` entry) so the whole install, not only the installer, is covered by the signature; (b) the first release has to be published (stage 1, "Left") and the updater tried by hand on this machine
   (a 1.0.1 over an installed 1.0.0, in use and inactive); (c) the old files stay on the server until installed builds have this updater.
4. **Done in the code (2026-10-30), tested against a fake backend on Windows, not tried on a real Linux AppImage.** `linux-shell/update_checker.py` is rewritten the same way as Windows': thin, no GTK in its core. `UpdateClient`
   polls `GET /api/update/status` every 30 s; `install` → copy the verified file beside the running AppImage, `os.replace` it (Linux lets a running file be replaced), tell the backend (`/api/update/installing`),
   start the new AppImage, ask the shell to exit; `ask` → the tray item and a status-bar line, **no dialog**; the tray click is the yes (`update_now` → `/api/update/confirm`). A failed copy leaves the old
   AppImage, says so in the status bar and tries again on the next poll. `UpdateChecker` is the GTK adapter with the interface `caroline_shell.py` already uses (`start`, `update_now`, the three callbacks), so the
   shell is unchanged. The earlier Yes/No dialog and the earlier download/sha256 code are gone (the backend does them). `backend-py/test_linux_update_client.py`, 7 tests against a fake HTTP backend (nothing ready,
   progress once per value, inactive installs, in use only offers, the yes installs, a failed copy retries, an unreachable backend is quiet); 28 update tests pass together. **Left:** the real run on Linux
   (an AppImage from `build_appimage.sh` updated over an older one: O1-O5 of the Xopek plan's section 6.1, now against the new flow) once a release is published.
5. **Done in the code (2026-10-30), tested on this machine, the image not built (no docker here).** A new platform in the manifest, **`xopek`**, and a new kind of artifact, **the code layer**: a zip of only the paths the
   update owns (`backend-py/app`, `backend-py/skills-src`, `wwwroot`, `linux-shell`, a few files, the sidecars' JS), no runtime and no `node_modules`. `backend-py/app/overlay_install.py` builds it
   (`tools/release.py overlay DIST OUT.zip`) and applies it: it refuses, before touching anything, a path that leaves the root, a link, anything not owned, a missing `backend-py/app/main.py`, or an unpacking
   past 600 MB; it swaps the directories one by one with the old one aside (**whole directories**, so a module the new version dropped is dropped too: plugins are found by listing their folder), puts
   everything back if any step fails, and repairs a crash in the middle at the next call (`recover`). `backend-py/app/update_cli.py` (`python -m app.update_cli apply`) is the start-time update: look, download,
   verify, apply, and **never fail the start** (offline, no release, a refused file: exit 0, Caroline stays as she is). `test_overlay_install.py`, 12 tests (build then apply keeps the runtime and
   `node_modules`, no caches in the zip, five kinds of bad overlay and a link refused with the dist unchanged, a failure in the middle rolls back, a crash is repaired, and the start-time update end to end
   against a real local server: a newer release installs, no release or a bad one leaves the old); 30 update tests pass with the earlier ones.
   **In the Xopek repo** (`desktop-image/`): `Dockerfile.selkies` gives the code folders of `/opt/caroline/dist-linux` to the session user (the runtime stays root's); `entrypoint-selkies.sh` runs the
   update before it starts the shell and turns the running backend's own update loop off (`CAROLINE_UPDATE_DISABLE=1`: an ephemeral desktop is not updated while it runs, the next one starts with the newest);
   `docs/CAROLINE_SW_SESSION_PLAN.md` records O8 as implemented. The persistent home is never touched, as decided on 2026-10-27. **Left:** a desktop built from the image and started against a published release
   (T-checks of the Xopek plan); an update of a **running** desktop (it would need the shell to restart Caroline; not planned, since desktops are short-lived); the release still rebuilds and pushes
   `:selkies-<date>` (the image carries the newest at its build, the start-time update covers the gap until the next build).
6. **Done (2026-10-30), tried live on a local simulator of its own (instance `upd`, since removed).** In IntelliCat (`Simulator/runtime/caroline_update.py`, `caroline.py`, `shelld.py`; `docs/caroline-updates.md`, commit ca27809).
   Her backend (platform `beroun`, `CAROLINE_RUNTIME` = the device's runtime number, `CAROLINE_UPDATE_DIR` in the owner's data) finds, downloads and verifies the artifact; the `Updater` in `shelld` polls her
   `/api/update/status` every 30 s and installs by the rule: `install` stops her, applies the overlay, starts her again; `ask` puts a line in the notification panel and a MessageBox ("Install now" / "Later", asked
   again after 4 h) and the yes is `POST /api/update/confirm`. The firmware's `dist-intellicat` is read-only and never touched; the update is applied to a writable copy (`<data>/caroline-code/current`, hard-linked
   where possible) which runs only while its `VERSION` is newer than the firmware's. **The way back:** an updated tree must answer on her port within 150 s and her supervisor must not die twice within a minute;
   otherwise its version is marked failed, the copy from before it (or the firmware's) runs again and the version is not tried or announced again; a tree that answered deletes the copy from before.
   Her tools `beroun_update_status|check|now` (`/api/caroline/sys/update/*`; the audit test passes). `tests/caroline_update_test.py`, 22 checks without a simulator. **Live, with the real signing key and a local
   release server:** 1.1.0 installed by itself 10 s after the start and ran from the copy (firmware stayed 1.0.0); a deliberately broken 1.2.0 was applied, never answered, and the device went back to 1.1.0 and
   left 1.2.0 alone; 1.3.0 then installed over it. The pin of `Simulator/caroline` moved to 34ff9da (the first with the update client). **Found on the way:** her `CAROLINE_RUNTIME` clashed with the shell's variable of
   the same name (the runtime's folder), so the backend gets the number explicitly; and a supervisor lives on while its backend crashes, so "the supervisor exited" alone cannot detect a broken update (hence the probation).
   **Left:** the "in use, so ask" path was not run live (a chat session must be open in her window on that instance; the rule is unit-tested and the MessageBox checked on its own); the hosted simulator (the same
   code runs at a visitor's machine start, but the image must be rebuilt with the new pin and tried); a first real release published (the maintainer's token) so the shipped devices find something.
7. **Android:** comes with the shell of `android-plan.md` (the same `CodeLayer`/`Updater` logic in the APK's shell: the overlay applied to a copy in the app's files directory, the dialog through the app's own UI); tried there.

Each stage: a release with a raised version published to a test path, installed while she is inactive (by itself) and while she is in use (the notice, then by itself once idle), a wrong
signature and a wrong sha256 refused, the data and the SW login kept.

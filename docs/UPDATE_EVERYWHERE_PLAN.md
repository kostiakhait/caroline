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

1. **The release:** `tools/release.sh` (every artifact from one commit), `release.json`, the signing key (made once, kept out of the repository), the upload, the old Windows files beside it.
2. **The activity** in `backend-py`: `GET /api/update/activity` and its reasons; tests.
3. **Windows:** the updater reads `release.json`, checks the signature, follows the rule; the MessageBox goes; the update at the start.
4. **Linux:** the same for the AppImage, then O1-O5 of the Xopek plan tried live at last.
5. **Xopek:** the update at the desktop's start and inside a running desktop; the release rebuilds the image.
6. **OS Beroun:** `shelld`'s updater, the folder beside the firmware's copy, the fallback, the notification, Caroline's tools; tried on the local simulator and the hosted one.
7. **Android:** comes with the shell of `android-plan.md`; tried there.

Each stage: a release with a raised version published to a test path, installed while she is inactive (by itself) and while she is in use (the notice, then by itself once idle), a wrong
signature and a wrong sha256 refused, the data and the SW login kept.

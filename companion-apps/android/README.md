# Caroline Companion (Android)

Phase 3 of the caroline-android-companion plan
(`C:\Users\khait\.claude\plans\caroline-android-companion.md`) — read that
file first for the full architecture/protocol context. This directory is a
**skeleton**, started 2026-09-10: a real, structured Gradle/Compose project
that builds and runs, not a finished app.

## What's actually here

- Gradle project (AGP 8.5.2, Kotlin 2.0.21, Compose BOM 2024.10.00, minSdk
  26) with the wrapper checked in (`./gradlew`).
- `data/remote/CamerlengoApi.kt` + `CamerlengoRepository.kt` — a real
  Retrofit/Moshi client for Camerlengo's `user:verify` (login) and
  `var:getMine`/`setMine`/`getAllMine`/`deleteMine` (the same commands
  `backend-py/app/plugins/companion_api.py` uses server-side). Field names
  and semantics are meant to match that file exactly — it's the spec.
- `ui/login/` — a working login screen (SquirrelWisdom email/password ->
  a v2 session, held in-memory only, see `SessionHolder`'s own doc comment
  for why that's not the final form).
- `ui/tabs/CompanionTabsScreen.kt` — a persistent top tab bar (matching the
  DESKTOP app's own tab strip, **not** a chat-list-then-push screen — that
  was an explicit correction during planning) populated from the backend's
  live `tabs_list` (never hardcoded).
- `ui/chat/ChatScreen.kt` — read-only iMessage-style bubbles pulling
  `tabs/<tabId>/history/*` directly over the network on open.

## What's explicitly NOT built yet

- **No local database.** Everything above is fetch-on-open over the
  network — none of the confirmed offline-first architecture (Room as the
  UI's source of truth, a WorkManager-driven background sync, real offline
  support) exists yet. See the plan's UI-direction section for what this
  is supposed to become (modeled on the real Ratatosk Android app's
  `ChatDao`/`MessageDao`/`ChatSyncWorker`).
- **No sending.** `tabs/<tabId>/inbox` (writing a phone-originated message
  into a tab) is unimplemented on this side.
- **No SMS/contacts integration at all** — no runtime permission flow, no
  consent screen, no `SmsReceiver`, no `content://sms` or `ContactsContract`
  queries, no foreground polling service. This is the bulk of what Phase 3
  still needs.
- **No persisted session** — a process restart loses the login; see
  `SessionHolder`'s own doc comment.
- **No real launcher icon / app identity art.**
- Not yet connected to CI, not yet signed, not yet run on a device or
  emulator in this environment (see Verification below).

## Verification

`./gradlew assembleDebug` succeeds cleanly (2026-09-10) and produces a real
`app-debug.apk` (~19.7MB). Installed and launched on a real emulator
(`Medium_Phone_API_36.0`, API 36): the process starts and stays alive (no
crash in logcat), and the login screen renders correctly, themed, with a
working email/password form. Not yet exercised past that — no login
attempt against a real account, no tab bar / chat screen seen live yet.
`local.properties` (machine-local SDK path) is gitignored; anyone building
this needs their own pointing at a real Android SDK.

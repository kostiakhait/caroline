package com.partnerssolutions.caroline.companion

import android.app.Application
import com.partnerssolutions.caroline.companion.data.companion.CompanionPrefs
import com.partnerssolutions.caroline.companion.data.companion.companionPermissionsGranted
import com.partnerssolutions.caroline.companion.data.remote.CredentialsStore
import com.partnerssolutions.caroline.companion.service.CompanionOpsService
import com.partnerssolutions.caroline.companion.service.CompanionWatchdogWorker
import com.partnerssolutions.caroline.companion.update.UpdateChecker
import com.partnerssolutions.caroline.companion.util.Logger

/**
 * Skeleton (2026-09-10) -- see companion-apps/android/README.md and the
 * caroline-android-companion plan for the confirmed architecture this is
 * meant to grow into: Room-DB-backed offline-first storage as the UI's
 * source of truth, a WorkManager-driven background sync against
 * Camerlengo's var:*Mine commands, an actual tab bar fed from the
 * backend-published `tabs_list` (never hardcoded).
 */
class CompanionApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        Logger.init(this)
        CredentialsStore.init(this)
        CompanionPrefs.init(this)
        // Re-start the SMS/contacts service on every process start if the
        // user previously opted in (CompanionSetupScreen) AND every
        // permission it needs is still actually granted -- a revoked
        // permission (Settings, or an OS-level auto-reset for an unused
        // app) must silently disable the feature again, never crash-loop
        // a service that can't do its job.
        if (CompanionPrefs.enabled && companionPermissionsGranted(this)) {
            CompanionOpsService.start(this)
        } else if (CompanionPrefs.enabled) {
            Logger.w("companion was enabled but a required permission is no longer granted -- disabling")
            CompanionPrefs.enabled = false
        }
        // Per explicit instruction (2026-09-30): a periodic backstop so a
        // service some OS timer/battery-optimizer quietly killed gets
        // restarted on its own schedule, not only when the user happens to
        // open the app or the process happens to restart. A no-op (see
        // CompanionWatchdogWorker's own doc comment) whenever the feature
        // is off or already healthy -- safe to always schedule.
        CompanionWatchdogWorker.schedule(this)
        // OTA self-update (2026-10-01), same pattern as ShortNerdCat's own
        // UpdateChecker -- runs for the lifetime of the process, independent
        // of whether the optional SMS/contacts companion feature is on.
        UpdateChecker(this).start()
        Logger.i("CompanionApplication started")
    }
}

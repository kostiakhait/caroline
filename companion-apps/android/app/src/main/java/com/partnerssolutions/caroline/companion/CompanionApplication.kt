package com.partnerssolutions.caroline.companion

import android.app.Application
import com.partnerssolutions.caroline.companion.data.companion.CompanionPrefs
import com.partnerssolutions.caroline.companion.data.companion.companionPermissionsGranted
import com.partnerssolutions.caroline.companion.data.remote.CredentialsStore
import com.partnerssolutions.caroline.companion.service.CompanionOpsService
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
        Logger.i("CompanionApplication started")
    }
}

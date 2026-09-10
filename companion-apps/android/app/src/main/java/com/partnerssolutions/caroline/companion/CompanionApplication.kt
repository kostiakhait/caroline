package com.partnerssolutions.caroline.companion

import android.app.Application
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
        Logger.i("CompanionApplication started")
    }
}

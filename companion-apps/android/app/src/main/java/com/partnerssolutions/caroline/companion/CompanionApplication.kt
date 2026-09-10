package com.partnerssolutions.caroline.companion

import android.app.Application

/**
 * Skeleton (2026-09-10) -- see companion-apps/android/README.md and the
 * caroline-android-companion plan for the confirmed architecture this is
 * meant to grow into: Room-DB-backed offline-first storage as the UI's
 * source of truth, a WorkManager-driven background sync against
 * Camerlengo's var:*Mine commands, an actual tab bar fed from the
 * backend-published `tabs_list` (never hardcoded).
 *
 * Nothing is wired up yet -- no DI container, no Room database instance,
 * no sync worker registration. This class exists so AndroidManifest.xml
 * has a real android:name to point at from day one, instead of adding it
 * later as a breaking change.
 */
class CompanionApplication : Application()

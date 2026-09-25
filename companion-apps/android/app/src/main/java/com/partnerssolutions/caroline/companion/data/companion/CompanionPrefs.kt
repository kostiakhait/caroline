package com.partnerssolutions.caroline.companion.data.companion

import android.Manifest
import android.content.Context
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.ContextCompat

/** Every runtime permission CompanionOpsService needs to do its job --
 * the one place this list is defined, used by both the setup screen (to
 * request them) and CompanionApplication (to re-check them on every
 * process start before auto-starting the service). */
val COMPANION_REQUIRED_PERMISSIONS: Array<String> = buildList {
    add(Manifest.permission.SEND_SMS)
    add(Manifest.permission.READ_SMS)
    add(Manifest.permission.READ_CONTACTS)
    // The persistent foreground-service notification needs this on 13+;
    // requesting it pre-33 too is a harmless no-op there.
    if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS)
}.toTypedArray()

fun companionPermissionsGranted(context: Context): Boolean =
    COMPANION_REQUIRED_PERMISSIONS.all { ContextCompat.checkSelfPermission(context, it) == PackageManager.PERMISSION_GRANTED }

/**
 * One persisted flag: has the user explicitly opted into the SMS/contacts
 * phone-companion feature (CompanionOpsService)? Plain (unencrypted)
 * SharedPreferences -- unlike CredentialsStore this holds no secret, just
 * a boolean the app checks on every process start to decide whether to
 * auto-start the foreground service (see CompanionApplication.onCreate).
 * Real permission grants are re-checked independently at that same
 * point -- this flag alone never bypasses Android's own permission system,
 * it only remembers "the user said yes" so they aren't re-asked the intro
 * screen every launch.
 */
object CompanionPrefs {
    private const val FILE_NAME = "companion_prefs"
    private const val KEY_ENABLED = "sms_contacts_enabled"

    private var prefs: SharedPreferences? = null

    fun init(context: Context) {
        prefs = context.getSharedPreferences(FILE_NAME, Context.MODE_PRIVATE)
    }

    var enabled: Boolean
        get() = prefs?.getBoolean(KEY_ENABLED, false) ?: false
        set(value) {
            prefs?.edit()?.putBoolean(KEY_ENABLED, value)?.apply()
        }
}

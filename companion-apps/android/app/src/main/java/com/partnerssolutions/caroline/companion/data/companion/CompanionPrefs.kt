package com.partnerssolutions.caroline.companion.data.companion

import android.Manifest
import android.content.Context
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.os.Build
import android.telephony.SubscriptionManager
import android.telephony.TelephonyManager
import androidx.core.content.ContextCompat
import java.util.UUID

/** Every runtime permission CompanionOpsService needs to do its job --
 * the one place this list is defined, used by both the setup screen (to
 * request them) and CompanionApplication (to re-check them on every
 * process start before auto-starting the service). READ_PHONE_NUMBERS is
 * here too (multi-phone support, 2026-09-26): best-effort auto-detection
 * of this phone's own number to prefill the setup screen's confirmation
 * field -- see CompanionPrefs.bestEffortDetectedNumber. */
val COMPANION_REQUIRED_PERMISSIONS: Array<String> = buildList {
    add(Manifest.permission.SEND_SMS)
    add(Manifest.permission.READ_SMS)
    add(Manifest.permission.READ_CONTACTS)
    if (Build.VERSION.SDK_INT >= 26) add(Manifest.permission.READ_PHONE_NUMBERS)
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
    private const val KEY_DEVICE_ID = "device_id"
    private const val KEY_PHONE_NUMBER = "phone_number"

    private var prefs: SharedPreferences? = null

    fun init(context: Context) {
        prefs = context.getSharedPreferences(FILE_NAME, Context.MODE_PRIVATE)
    }

    var enabled: Boolean
        get() = prefs?.getBoolean(KEY_ENABLED, false) ?: false
        set(value) {
            prefs?.edit()?.putBoolean(KEY_ENABLED, value)?.apply()
        }

    /**
     * This install's own stable identity for the multi-phone companion
     * protocol (explicit instruction, 2026-09-26) -- a random UUID
     * generated once and persisted forever, NOT the phone number: Android
     * frequently can't report a real number at all (carrier-dependent,
     * often blank even with every relevant permission granted), so a
     * stable key can't be built on data that unreliable. Every var: path
     * this phone touches lives under devices/<deviceId>/... so multiple
     * phones paired to one account never race on the same path.
     */
    val deviceId: String
        get() {
            val p = prefs ?: return UUID.randomUUID().toString() // init() not called yet -- shouldn't happen, but never crash
            p.getString(KEY_DEVICE_ID, null)?.let { return it }
            val fresh = UUID.randomUUID().toString()
            p.edit().putString(KEY_DEVICE_ID, fresh).apply()
            return fresh
        }

    /** The user-CONFIRMED number for this phone, set by CompanionSetupScreen
     * -- null until setup has been completed at least once. This is what
     * gets published in this device's own heartbeat (devices/<deviceId>/
     * info) and what Caroline's fromNumber matching is done against.
     * Never set silently from bestEffortDetectedNumber without the user
     * having seen/confirmed it in the setup screen's text field. */
    var phoneNumber: String?
        get() = prefs?.getString(KEY_PHONE_NUMBER, null)
        set(value) {
            prefs?.edit()?.putString(KEY_PHONE_NUMBER, value?.trim()?.takeIf { it.isNotEmpty() })?.apply()
        }

    /**
     * Best-effort OS-reported number, to PREFILL the setup screen's
     * confirmation field -- never used as `phoneNumber` directly, because
     * getLine1Number()/SubscriptionManager frequently return null or an
     * empty string depending on carrier and SIM state, even when
     * READ_PHONE_NUMBERS is granted. Returns null on any failure or
     * missing permission rather than throwing.
     */
    fun bestEffortDetectedNumber(context: Context): String? {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.READ_PHONE_NUMBERS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            return null
        }
        return try {
            if (Build.VERSION.SDK_INT >= 33) {
                val subManager = context.getSystemService(SubscriptionManager::class.java)
                val defaultSubId = SubscriptionManager.getDefaultSubscriptionId()
                subManager?.getPhoneNumber(defaultSubId)?.takeIf { it.isNotBlank() }
            } else {
                @Suppress("DEPRECATION")
                val tm = context.getSystemService(TelephonyManager::class.java)
                tm?.line1Number?.takeIf { it.isNotBlank() }
            }
        } catch (_: Exception) {
            null
        }
    }
}

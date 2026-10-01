package com.partnerssolutions.caroline.companion.data.companion

import android.Manifest
import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import android.telephony.SubscriptionManager
import android.telephony.TelephonyManager
import androidx.core.content.ContextCompat
import com.partnerssolutions.caroline.companion.service.CompanionOpsService
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
    // companion_create_contact (2026-09-30).
    add(Manifest.permission.WRITE_CONTACTS)
    if (Build.VERSION.SDK_INT >= 26) add(Manifest.permission.READ_PHONE_NUMBERS)
    // The persistent foreground-service notification needs this on 13+;
    // requesting it pre-33 too is a harmless no-op there.
    if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS)
}.toTypedArray()

fun companionPermissionsGranted(context: Context): Boolean =
    COMPANION_REQUIRED_PERMISSIONS.all { ContextCompat.checkSelfPermission(context, it) == PackageManager.PERMISSION_GRANTED }

/**
 * Whether CompanionOpsService is ACTUALLY alive right now, not just
 * whether CompanionPrefs.enabled says it should be. Per explicit
 * instruction (2026-09-30), after a real incident: the foreground service
 * can die (OEM battery optimizer, Doze, plain system pressure despite
 * START_STICKY/onTaskRemoved/BootReceiver) without CompanionPrefs.enabled
 * ever getting cleared -- so every call site that only checked `enabled`
 * before deciding "nothing to do" was silently trusting a flag that can
 * go stale. getRunningServices is deprecated for inspecting OTHER apps'
 * services (Android locked that down from API 26), but querying this
 * app's OWN service by class name still works reliably -- confirmed
 * against AOSP's own source: the restriction is about cross-app
 * visibility, not self-visibility.
 */
@Suppress("DEPRECATION")
fun isCompanionServiceRunning(context: Context): Boolean {
    val am = context.getSystemService(ActivityManager::class.java) ?: return false
    return am.getRunningServices(Int.MAX_VALUE).any { it.service.className == CompanionOpsService::class.java.name }
}

/** Starts CompanionOpsService if CompanionPrefs.enabled is true but the
 * service isn't actually running -- the one check that matters for both
 * "disabled by some OS timer/policy" and "user just brought Caroline back
 * to the foreground" (per explicit instruction, 2026-09-30: restart on
 * both). A no-op (returns false) when permissions aren't currently
 * granted -- nothing to restart into, same as every other call site's
 * existing permission gate. Safe to call from any context; CompanionOps
 * Service.start() itself is idempotent (CompanionOpsService.onStartCommand's
 * own pollJob guard) so calling this when the service is already healthy
 * does nothing extra. */
fun restartCompanionServiceIfNeeded(context: Context): Boolean {
    if (!CompanionPrefs.enabled) return false
    if (isCompanionServiceRunning(context)) return false
    if (!companionPermissionsGranted(context)) return false
    CompanionOpsService.start(context)
    return true
}

/**
 * Turns the SMS/contacts companion on: saves [phoneNumber], marks it
 * enabled, starts CompanionOpsService, and (best-effort) asks the user to
 * exempt the app from battery optimization. Shared by the automatic
 * first-launch activation (CompanionTabsScreen) and CompanionSetupScreen's
 * own manual re-enable path (e.g. after the user had switched it off), so
 * there is exactly one place that does this, not two copies drifting apart.
 *
 * Per explicit instruction (2026-09-27), reversing the 2026-09-24 opt-in
 * decision: the feature now activates itself automatically once Android's
 * OWN permission dialogs are granted, instead of waiting for the user to
 * find CompanionSetupScreen and flip a switch. Calling this does NOT
 * itself request permissions -- callers must already hold them (see
 * companionPermissionsGranted) or be inside a permission-grant callback.
 */
fun activateCompanionService(context: Context, phoneNumber: String) {
    CompanionPrefs.phoneNumber = phoneNumber
    CompanionPrefs.enabled = true
    CompanionOpsService.start(context)
    val powerManager = context.getSystemService(PowerManager::class.java)
    if (powerManager?.isIgnoringBatteryOptimizations(context.packageName) == false) {
        context.startActivity(
            Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:${context.packageName}")),
        )
    }
}

/**
 * One persisted flag: is the SMS/contacts phone-companion feature
 * (CompanionOpsService) currently on? Plain (unencrypted) SharedPreferences
 * -- unlike CredentialsStore this holds no secret, just a boolean the app
 * checks on every process start to decide whether to auto-start the
 * foreground service (see CompanionApplication.onCreate).
 *
 * Per explicit instruction (2026-09-27): this used to mean "the user
 * explicitly opted in" (CompanionSetupScreen's switch was the only way to
 * flip it true) -- now CompanionTabsScreen sets it automatically the first
 * time Android's own permission dialogs are granted (see
 * activateCompanionService), so it instead means "the feature is currently
 * running" -- still user-overridable (the switch can turn it back off),
 * just no longer gated behind finding a menu first. Real permission grants
 * are re-checked independently on every process start regardless of this
 * flag's value -- it alone never bypasses Android's own permission system.
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

    /** This phone's number, published in this device's own heartbeat
     * (devices/<deviceId>/info) and what Caroline's fromNumber matching is
     * done against -- null until it's been set at least once, either
     * automatically (CompanionTabsScreen re-detects and saves this on every
     * launch via bestEffortDetectedNumber, per explicit instruction
     * 2026-09-27) or manually (CompanionSetupScreen's text field, which
     * always wins for the rest of that launch -- the auto-detect-on-launch
     * pass is what can override it again, not anything mid-session). */
    var phoneNumber: String?
        get() = prefs?.getString(KEY_PHONE_NUMBER, null)
        set(value) {
            prefs?.edit()?.putString(KEY_PHONE_NUMBER, value?.trim()?.takeIf { it.isNotEmpty() })?.apply()
        }

    /**
     * Best-effort OS-reported number. Per explicit instruction (2026-09-27)
     * this now feeds `phoneNumber` automatically (CompanionTabsScreen, on
     * every launch) rather than only prefilling a field the user had to
     * confirm -- but it stays best-effort, not authoritative:
     * getLine1Number()/SubscriptionManager frequently return null or an
     * empty string depending on carrier and SIM state, even when
     * READ_PHONE_NUMBERS is granted, which is exactly why a null result
     * here leaves `phoneNumber` alone rather than clearing it -- a manual
     * entry from a previous launch survives until detection actually
     * produces something to replace it with. Returns null on any failure
     * or missing permission rather than throwing.
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

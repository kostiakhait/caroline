package com.partnerssolutions.caroline.companion.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.partnerssolutions.caroline.companion.data.companion.CompanionPrefs
import com.partnerssolutions.caroline.companion.data.companion.companionPermissionsGranted
import com.partnerssolutions.caroline.companion.util.Logger

/**
 * Restarts the companion service after a device reboot, or after the app
 * is updated (Android delivers MY_PACKAGE_REPLACED, which also kills any
 * running service) -- so SMS/contacts access resumes without the user
 * having to open the app first. Ported from Ratatosk's own BootReceiver,
 * which solved the exact same problem.
 *
 * Starting a foreground service from BOOT_COMPLETED is one of the
 * documented exemptions to Android 12+'s background-start restrictions,
 * so this is safe even though there's no activity involved.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED && intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        CompanionPrefs.init(context)
        if (CompanionPrefs.enabled && companionPermissionsGranted(context)) {
            Logger.i("BootReceiver: starting CompanionOpsService (action=${intent.action})")
            CompanionOpsService.start(context)
        } else {
            Logger.i("BootReceiver: not starting (companion disabled or a permission was revoked)")
        }
    }
}

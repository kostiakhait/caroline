package com.partnerssolutions.caroline.companion.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.partnerssolutions.caroline.companion.data.companion.CompanionPrefs
import com.partnerssolutions.caroline.companion.data.companion.companionPermissionsGranted
import com.partnerssolutions.caroline.companion.util.Logger

/**
 * Triggered by AlarmManager from CompanionOpsService.onTaskRemoved() --
 * restarts the service ~5s after the user swipes the app away. Works
 * around OEM battery optimizers that ignore START_STICKY after
 * onTaskRemoved (common on Xiaomi/Huawei/Samsung ROMs) -- ported from
 * Ratatosk's own ServiceRestartReceiver, which solved the exact same
 * problem for its own always-on sync service.
 *
 * Gated the same way CompanionApplication.onCreate already is: only
 * restarts if the user is still opted in AND every permission the
 * service needs is still actually granted.
 */
class ServiceRestartReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        CompanionPrefs.init(context)
        if (CompanionPrefs.enabled && companionPermissionsGranted(context)) {
            Logger.i("ServiceRestartReceiver: restarting CompanionOpsService")
            CompanionOpsService.start(context)
        } else {
            Logger.i("ServiceRestartReceiver: not restarting (companion disabled or a permission was revoked)")
        }
    }
}

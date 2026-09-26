package com.partnerssolutions.caroline.companion.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.partnerssolutions.caroline.companion.data.companion.CompanionPrefs
import com.partnerssolutions.caroline.companion.data.contacts.ContactsRepository
import com.partnerssolutions.caroline.companion.data.remote.CamerlengoRepository
import com.partnerssolutions.caroline.companion.data.sms.SmsRepository
import com.partnerssolutions.caroline.companion.ui.MainActivity
import com.partnerssolutions.caroline.companion.util.Logger
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * Foreground service: the phone half of companion_plugin.py's two-phase
 * accept/result protocol (see companion_api.py's own module docstring for
 * the full protocol and CompanionOpsService's own poll loop below for the
 * phone side of it). Runs only while the user has explicitly opted in
 * (CompanionPrefs.enabled) -- started from the setup screen once
 * permissions are granted, and again from CompanionApplication.onCreate
 * on every process start if still enabled.
 *
 * Idempotent/crash-safe by construction: before acting on any request, it
 * checks whether that request's OWN result path already has a value --
 * if so, a previous run (or an earlier tick still racing backend cleanup)
 * already finished it, so it's skipped rather than redone (critical for
 * companion_sms_send specifically -- redoing it would send a duplicate
 * real text message). No local "already handled" tracking needed at all.
 *
 * Multi-phone support (explicit instruction, 2026-09-26): every path this
 * service reads/writes lives under devices/<deviceId>/... (CompanionPrefs.
 * deviceId, a random UUID generated once per install) instead of a flat
 * shared path -- so a second phone paired to the same account watches its
 * OWN subtree and never races this one on the same request. This service
 * also periodically writes its own devices/<deviceId>/info heartbeat
 * (phone number + last-seen time) -- companion_api.py's device registry
 * on the backend reads that to know what's paired and how to address it.
 *
 * Resilience strategy (ported from Ratatosk's ChatSyncService, the same
 * problem solved before -- explicit instruction, 2026-09-26: "нет
 * постоянной иконки сервиса, следовательно нет и постоянно работающего
 * сервиса", confirmed live -- this service WAS running, but
 * IMPORTANCE_MIN made its notification -- and the status-bar icon that's
 * the user's only visible proof it's running -- invisible on at least some
 * OEM skins, which read as "the service isn't there at all"):
 *   1. START_STICKY -- Android re-creates the service after a system-pressure kill.
 *   2. onTaskRemoved + AlarmManager -- schedules an explicit restart 5s after
 *      the user swipes the app away, bypassing OEM battery optimizers that
 *      ignore START_STICKY (common on Xiaomi/Huawei/Samsung).
 *   3. BootReceiver (separate file) -- restarts after reboot / package replace.
 *   4. Battery-optimization exemption prompt (CompanionSetupScreen, when the
 *      user turns the feature on) -- reduces the odds of #2 being needed at all.
 */
class CompanionOpsService : Service() {

    private val scope = CoroutineScope(Dispatchers.IO + Job())
    private val repository = CamerlengoRepository()
    private lateinit var smsRepository: SmsRepository
    private lateinit var contactsRepository: ContactsRepository
    private var lastHeartbeatAt = 0L

    override fun onCreate() {
        super.onCreate()
        smsRepository = SmsRepository(applicationContext)
        contactsRepository = ContactsRepository(applicationContext)
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIFICATION_ID, buildNotification())
        Logger.i("CompanionOpsService started (deviceId=${CompanionPrefs.deviceId})")
        scope.launch { pollLoop() }
        return START_STICKY
    }

    // Called when the user swipes the app away from recents. On many OEM ROMs
    // START_STICKY is ignored after this event; AlarmManager provides a
    // guaranteed fallback restart (ported from Ratatosk's own fix for the
    // exact same problem).
    override fun onTaskRemoved(rootIntent: Intent?) {
        if (CompanionPrefs.enabled) {
            val restartIntent = Intent(applicationContext, ServiceRestartReceiver::class.java).apply { setPackage(packageName) }
            val pi = PendingIntent.getBroadcast(
                applicationContext, 1, restartIntent,
                PendingIntent.FLAG_ONE_SHOT or PendingIntent.FLAG_IMMUTABLE,
            )
            getSystemService(android.app.AlarmManager::class.java).set(
                android.app.AlarmManager.ELAPSED_REALTIME_WAKEUP,
                android.os.SystemClock.elapsedRealtime() + 5_000L,
                pi,
            )
            Logger.i("CompanionOpsService.onTaskRemoved: restart scheduled in 5s")
        }
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        super.onDestroy()
        scope.coroutineContext[Job]?.cancel()
        Logger.i("CompanionOpsService stopped")
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private suspend fun pollLoop() {
        while (scope.isActive) {
            try {
                handleHeartbeat()
                handleOutbox()
                handleSmsSync()
                handleRequestFamily("contacts")
            } catch (exc: Exception) {
                // A single bad tick (a transient network error, say) must
                // never kill the loop -- there's no other recovery path
                // for a foreground service silently dying but staying
                // "running" in the user's notification shade.
                Logger.e("CompanionOpsService poll tick failed", exc)
            }
            delay(POLL_INTERVAL_MS)
        }
    }

    // --- devices/<deviceId>/info: presence + number announcement -----------
    // Throttled to HEARTBEAT_INTERVAL_MS, not written on every 5s poll tick
    // -- the backend's own staleness check (DEVICE_STALE_AFTER_S=300s,
    // companion_api.py) has plenty of margin over this cadence.

    private suspend fun handleHeartbeat() {
        val now = System.currentTimeMillis()
        if (now - lastHeartbeatAt < HEARTBEAT_INTERVAL_MS) return
        repository.setMine(
            "devices/${CompanionPrefs.deviceId}/info",
            mapOf("phoneNumber" to CompanionPrefs.phoneNumber, "model" to Build.MODEL, "lastSeenAt" to now),
        )
        lastHeartbeatAt = now
    }

    // --- devices/<deviceId>/sms/outbox: companion_sms_send -----------------

    private suspend fun handleOutbox() {
        val base = "devices/${CompanionPrefs.deviceId}/sms/outbox"
        val entries = repository.getAllMine(base)
        for ((rawId, rawValue) in entries) {
            val opId = rawId as? String ?: continue
            val payload = rawValue as? Map<*, *> ?: continue
            val resultPath = "devices/${CompanionPrefs.deviceId}/sms/outbox_result/$opId"
            if (repository.getMine(resultPath) != null) continue // already done, backend hasn't cleaned up yet
            val requestPath = "$base/$opId"
            if (payload["status"] != "accepted") {
                repository.setMine(requestPath, withAcceptedStatus(payload))
            }
            val to = (payload["to"] as? String).orEmpty()
            val text = (payload["text"] as? String).orEmpty()
            Logger.i("companion: sending SMS to $to (opId=$opId)")
            val outcome = smsRepository.send(to, text)
            val result: Map<String, Any?> = outcome.fold(
                onSuccess = { mapOf("ok" to true) },
                onFailure = { mapOf("ok" to false, "error" to (it.message ?: it.toString())) },
            )
            repository.setMine(resultPath, result)
        }
    }

    // --- devices/<deviceId>/sms/sync_request -> sync_response: local-copy
    // sync. Per explicit instruction (2026-09-25): companion_list_sms_
    // threads/companion_read_sms_thread no longer talk to a phone live at
    // all -- Caroline's backend keeps its OWN local copy, refreshed by this
    // exchange every ~3 minutes (companion_api.py's start_sms_sync_loop),
    // separately per paired device. Single leaf paths under this device's
    // own subtree (not a per-opId family like the others below): only one
    // sync is ever in flight per device at a time. Same idempotency
    // principle as everywhere else -- skip if we've already answered the
    // current request.

    private suspend fun handleSmsSync() {
        val base = "devices/${CompanionPrefs.deviceId}/sms"
        val request = repository.getMine("$base/sync_request") as? Map<*, *> ?: return
        if (repository.getMine("$base/sync_response") != null) return // already answered, backend hasn't cleaned up yet
        val since = (request["since"] as? Number)?.toLong()
        Logger.i("companion: syncing SMS since=$since")
        val messages = smsRepository.dumpMessages(since)
        repository.setMine("$base/sync_response", mapOf("messages" to messages))
    }

    // --- devices/<deviceId>/{family}/requests -> responses: contacts lookups

    private suspend fun handleRequestFamily(family: String) {
        val base = "devices/${CompanionPrefs.deviceId}/$family"
        val entries = repository.getAllMine("$base/requests")
        for ((rawId, rawValue) in entries) {
            val opId = rawId as? String ?: continue
            val payload = rawValue as? Map<*, *> ?: continue
            val responsePath = "$base/responses/$opId"
            if (repository.getMine(responsePath) != null) continue
            val requestPath = "$base/requests/$opId"
            if (payload["status"] != "accepted") {
                repository.setMine(requestPath, withAcceptedStatus(payload))
            }
            val op = payload["op"] as? String
            Logger.i("companion: handling $family request op=$op (opId=$opId)")
            val result: Any = try {
                if (family == "contacts") handleContactsOp(op, payload) else mapOf("error" to "unknown family $family")
            } catch (exc: Exception) {
                Logger.e("companion: $family op=$op failed", exc)
                mapOf("error" to (exc.message ?: exc.toString()))
            }
            repository.setMine(responsePath, result)
        }
    }

    /** Map<*, *>'s own `+` operator can't be used directly (K is a star
     * projection, so the compiler can't verify a String key fits) -- build
     * a properly-typed copy instead. Keys here are always the JSON field
     * names the backend wrote (always String), never anything else. */
    private fun withAcceptedStatus(payload: Map<*, *>): Map<String, Any?> =
        payload.entries.associate { (k, v) -> (k as String) to v } + ("status" to "accepted")

    private fun handleContactsOp(op: String?, payload: Map<*, *>): Any = when (op) {
        "list" -> contactsRepository.list().map { mapOf("name" to it.name, "numbers" to it.numbers) }
        "search" -> {
            val query = (payload["query"] as? String).orEmpty()
            contactsRepository.search(query).map { mapOf("name" to it.name, "numbers" to it.numbers) }
        }
        else -> mapOf("error" to "unknown contacts op '$op'")
    }

    // --- foreground notification --------------------------------------------

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        // IMPORTANCE_LOW + ongoing = persistent but silent status-bar icon (no heads-up
        // popup, no sound) -- IMPORTANCE_MIN (the previous setting) goes further and
        // suppresses the status-bar icon entirely on at least some OEM skins, which is
        // indistinguishable from "the service isn't running" to the user. Channel
        // importance is user/system-owned once created and never changes after the
        // fact, so CHANNEL_ID also had to change (same fix Ratatosk's own
        // chat_sync_v2 channel made) -- an install that already created the old
        // "companion_ops" channel at MIN would otherwise stay stuck there forever.
        val channel = NotificationChannel(
            CHANNEL_ID, "Caroline phone companion", NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = "Watches for SMS/contacts requests from your desktop Caroline."
            setShowBadge(false)
        }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun buildNotification(): Notification {
        val openApp = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Caroline phone companion")
            .setContentText("Watching for SMS/contacts requests from your desktop.")
            // Stock system icon -- same "no real art yet" placeholder the
            // manifest's own launcher icon already uses (TODO there too).
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setContentIntent(openApp)
            .build()
    }

    companion object {
        // _v2: renamed off "companion_ops" when its importance bumped from MIN to LOW
        // (see createNotificationChannel's own comment) -- an existing install keeps
        // whatever importance it already created a channel at, forever.
        private const val CHANNEL_ID = "companion_ops_v2"
        private const val NOTIFICATION_ID = 1001

        // Well under PHASE1_POLL_INTERVAL_S=60s (companion_api.py) so an
        // accept lands promptly -- the desktop's own 60s poll is what
        // dominates end-to-end latency either way, this just avoids being
        // an ADDITIONAL bottleneck.
        private const val POLL_INTERVAL_MS = 5_000L

        // Comfortably under companion_api.py's DEVICE_STALE_AFTER_S=300s,
        // and no reason to write it on every 5s poll tick -- a device's
        // number/model essentially never changes between heartbeats.
        private const val HEARTBEAT_INTERVAL_MS = 60_000L

        fun start(context: Context) {
            val intent = Intent(context, CompanionOpsService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(intent)
            else context.startService(intent)
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, CompanionOpsService::class.java))
        }
    }
}

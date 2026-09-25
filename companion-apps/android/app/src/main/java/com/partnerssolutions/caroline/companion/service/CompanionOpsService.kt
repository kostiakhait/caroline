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
 */
class CompanionOpsService : Service() {

    private val scope = CoroutineScope(Dispatchers.IO + Job())
    private val repository = CamerlengoRepository()
    private lateinit var smsRepository: SmsRepository
    private lateinit var contactsRepository: ContactsRepository

    override fun onCreate() {
        super.onCreate()
        smsRepository = SmsRepository(applicationContext)
        contactsRepository = ContactsRepository(applicationContext)
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIFICATION_ID, buildNotification())
        Logger.i("CompanionOpsService started")
        scope.launch { pollLoop() }
        return START_STICKY
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

    // --- sms/outbox: companion_sms_send ------------------------------------

    private suspend fun handleOutbox() {
        val entries = repository.getAllMine("sms/outbox")
        for ((rawId, rawValue) in entries) {
            val opId = rawId as? String ?: continue
            val payload = rawValue as? Map<*, *> ?: continue
            val resultPath = "sms/outbox_result/$opId"
            if (repository.getMine(resultPath) != null) continue // already done, backend hasn't cleaned up yet
            val requestPath = "sms/outbox/$opId"
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

    // --- sms/sync_request -> sms/sync_response: local-copy sync -----------
    // Per explicit instruction (2026-09-25): companion_list_sms_threads/
    // companion_read_sms_thread no longer talk to the phone live at all --
    // Caroline's backend keeps its OWN local copy, refreshed by this
    // exchange every ~3 minutes (companion_api.py's start_sms_sync_loop).
    // Single leaf paths (not a per-opId family like the others below):
    // only one sync is ever in flight at a time. Same idempotency
    // principle as everywhere else -- skip if we've already answered the
    // current request.

    private suspend fun handleSmsSync() {
        val request = repository.getMine("sms/sync_request") as? Map<*, *> ?: return
        if (repository.getMine("sms/sync_response") != null) return // already answered, backend hasn't cleaned up yet
        val since = (request["since"] as? Number)?.toLong()
        Logger.i("companion: syncing SMS since=$since")
        val messages = smsRepository.dumpMessages(since)
        repository.setMine("sms/sync_response", mapOf("messages" to messages))
    }

    // --- {family}/requests -> {family}/responses: contacts lookups --------

    private suspend fun handleRequestFamily(family: String) {
        val entries = repository.getAllMine("$family/requests")
        for ((rawId, rawValue) in entries) {
            val opId = rawId as? String ?: continue
            val payload = rawValue as? Map<*, *> ?: continue
            val responsePath = "$family/responses/$opId"
            if (repository.getMine(responsePath) != null) continue
            val requestPath = "$family/requests/$opId"
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
        val channel = NotificationChannel(
            CHANNEL_ID, "Caroline phone companion", NotificationManager.IMPORTANCE_MIN,
        ).apply { description = "Watches for SMS/contacts requests from your desktop Caroline." }
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
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .setContentIntent(openApp)
            .build()
    }

    companion object {
        private const val CHANNEL_ID = "companion_ops"
        private const val NOTIFICATION_ID = 1001

        // Well under PHASE1_POLL_INTERVAL_S=60s (companion_api.py) so an
        // accept lands promptly -- the desktop's own 60s poll is what
        // dominates end-to-end latency either way, this just avoids being
        // an ADDITIONAL bottleneck.
        private const val POLL_INTERVAL_MS = 5_000L

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

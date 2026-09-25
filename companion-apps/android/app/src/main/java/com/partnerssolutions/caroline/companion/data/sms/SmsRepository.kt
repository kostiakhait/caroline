package com.partnerssolutions.caroline.companion.data.sms

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import android.provider.Telephony
import android.telephony.SmsManager
import com.partnerssolutions.caroline.companion.util.Logger
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import kotlin.coroutines.resume

/**
 * Real SMS on the user's own SIM via the standard (non-default-SMS-app)
 * surface: SmsManager to send, the Telephony content provider to read --
 * matches the plan's own explicit choice not to become the default SMS
 * app. dumpMessages() feeds Caroline's own LOCAL copy (companion_sms_
 * store.py on the backend) -- per explicit instruction (2026-09-25), a
 * phone isn't reliably reachable the way an IMAP server is, so
 * companion_list_sms_threads/companion_read_sms_thread never query this
 * phone live; CompanionOpsService's handleSmsSync() calls this
 * periodically instead and ships the result to the backend to merge.
 */
class SmsRepository(private val context: Context) {

    // How many messages a single sync answer can carry -- applies to a
    // bootstrap sync (since=null, the very first one) where "everything"
    // could otherwise mean a phone's entire multi-year SMS history in one
    // var:setMine call. An incremental sync (since set) is realistically
    // always far under this on a 3-minute cadence.
    private val BOOTSTRAP_LIMIT = 2000

    /**
     * Every message with date > since (all of them, capped, if since is
     * null -- the first-ever sync), oldest first. Shape matches what
     * companion_sms_store.py's merge_messages()/list_threads()/
     * list_messages() expect: threadId/address/body/date/type/read.
     */
    fun dumpMessages(since: Long?): List<Map<String, Any?>> {
        val out = mutableListOf<Map<String, Any?>>()
        val projection = arrayOf(
            Telephony.Sms.THREAD_ID, Telephony.Sms.ADDRESS, Telephony.Sms.BODY,
            Telephony.Sms.DATE, Telephony.Sms.TYPE, Telephony.Sms.READ,
        )
        val selection = if (since != null) "${Telephony.Sms.DATE} > ?" else null
        val args = if (since != null) arrayOf(since.toString()) else null
        val limit = if (since != null) Int.MAX_VALUE else BOOTSTRAP_LIMIT
        context.contentResolver.query(
            Telephony.Sms.CONTENT_URI, projection, selection, args, "${Telephony.Sms.DATE} DESC",
        )?.use { c ->
            val threadIdx = c.getColumnIndexOrThrow(Telephony.Sms.THREAD_ID)
            val addressIdx = c.getColumnIndexOrThrow(Telephony.Sms.ADDRESS)
            val bodyIdx = c.getColumnIndexOrThrow(Telephony.Sms.BODY)
            val dateIdx = c.getColumnIndexOrThrow(Telephony.Sms.DATE)
            val typeIdx = c.getColumnIndexOrThrow(Telephony.Sms.TYPE)
            val readIdx = c.getColumnIndexOrThrow(Telephony.Sms.READ)
            while (c.moveToNext() && out.size < limit) {
                // TYPE 1 = inbox (received), 2 = sent -- the two that matter
                // for a real conversation; drafts/failed/queued (3-6) are
                // rare and not worth a richer label here.
                val type = if (c.getInt(typeIdx) == Telephony.Sms.MESSAGE_TYPE_INBOX) "inbox" else "sent"
                out.add(
                    mapOf(
                        "threadId" to (c.getString(threadIdx) ?: continue),
                        "address" to c.getString(addressIdx),
                        "body" to (c.getString(bodyIdx) ?: ""),
                        "date" to c.getLong(dateIdx),
                        "type" to type,
                        "read" to (c.getInt(readIdx) != 0),
                    ),
                )
            }
        }
        return out
    }

    /**
     * Sends via SmsManager, splitting into multiple parts for a long body
     * (divideMessage), and waits (bounded) for the LAST part's own SENT
     * broadcast to know whether it actually went out. A real send can
     * legitimately take a few seconds (radio/carrier round trip) -- this
     * is a one-shot wait, not a poll loop, since SmsManager's own
     * PendingIntent callback is the authoritative signal.
     */
    suspend fun send(to: String, text: String): Result<Unit> {
        val smsManager = if (Build.VERSION.SDK_INT >= 31) context.getSystemService(SmsManager::class.java)
        else @Suppress("DEPRECATION") SmsManager.getDefault()
        val parts = smsManager.divideMessage(text)
        val action = "com.partnerssolutions.caroline.companion.SMS_SENT_${System.nanoTime()}"
        // One shared receiver, one callback per part (multi-part messages
        // are common past ~160 chars) -- resolves failure on the FIRST bad
        // result code, success only once EVERY part has reported ok.
        var remaining = parts.size
        val outcome = withTimeoutOrNull(20_000L) {
            suspendCancellableCoroutine<Result<Unit>> { cont ->
                val receiver = object : BroadcastReceiver() {
                    override fun onReceive(ctx: Context, intent: Intent) {
                        val ok = resultCode == android.app.Activity.RESULT_OK
                        if (!ok) {
                            try { context.unregisterReceiver(this) } catch (_: Exception) {}
                            if (cont.isActive) cont.resume(Result.failure(Exception("SmsManager result code $resultCode")))
                            return
                        }
                        remaining--
                        if (remaining <= 0) {
                            try { context.unregisterReceiver(this) } catch (_: Exception) {}
                            if (cont.isActive) cont.resume(Result.success(Unit))
                        }
                    }
                }
                val flags = if (Build.VERSION.SDK_INT >= 33) Context.RECEIVER_NOT_EXPORTED else 0
                if (Build.VERSION.SDK_INT >= 33) context.registerReceiver(receiver, IntentFilter(action), flags)
                else @Suppress("UnspecifiedRegisterReceiverFlag") context.registerReceiver(receiver, IntentFilter(action))
                val sentIntents = ArrayList<android.app.PendingIntent>()
                for (i in parts.indices) {
                    sentIntents.add(
                        android.app.PendingIntent.getBroadcast(
                            context, i, Intent(action),
                            android.app.PendingIntent.FLAG_UPDATE_CURRENT or android.app.PendingIntent.FLAG_IMMUTABLE,
                        ),
                    )
                }
                cont.invokeOnCancellation { try { context.unregisterReceiver(receiver) } catch (_: Exception) {} }
                try {
                    smsManager.sendMultipartTextMessage(to, null, parts, sentIntents, null)
                } catch (exc: Exception) {
                    Logger.e("SmsManager.sendMultipartTextMessage threw", exc)
                    try { context.unregisterReceiver(receiver) } catch (_: Exception) {}
                    if (cont.isActive) cont.resume(Result.failure(exc))
                }
            }
        }
        return outcome ?: Result.failure(Exception("Timed out waiting for the SMS radio to confirm the send."))
    }
}

package com.partnerssolutions.caroline.companion.data.sms

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.database.Cursor
import android.net.Uri
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
        val start = System.currentTimeMillis()
        val out = mutableListOf<Map<String, Any?>>()
        val projection = arrayOf(
            Telephony.Sms.THREAD_ID, Telephony.Sms.ADDRESS, Telephony.Sms.BODY,
            Telephony.Sms.DATE, Telephony.Sms.TYPE, Telephony.Sms.READ,
        )
        val selection = if (since != null) "${Telephony.Sms.DATE} > ?" else null
        val args = if (since != null) arrayOf(since.toString()) else null
        val limit = if (since != null) Int.MAX_VALUE else BOOTSTRAP_LIMIT
        val cursorFound = context.contentResolver.query(
            Telephony.Sms.CONTENT_URI, projection, selection, args, "${Telephony.Sms.DATE} DESC",
        )?.use { c -> readRows(c, limit).forEach { out.add(it) }; true } ?: false
        if (!cursorFound) Logger.w("SmsRepository.dumpMessages: content resolver query returned null cursor")
        Logger.i("SmsRepository.dumpMessages(since=$since): ${out.size} row(s) in ${System.currentTimeMillis() - start}ms")
        return out
    }

    /**
     * companion_search_sms (2026-09-30): queries the phone's real SMS
     * table LIVE, right now -- unlike dumpMessages (feeds the backend's
     * own periodic local-copy sync, see this file's own header comment),
     * this is for the case the local copy doesn't have an answer for
     * (not yet synced, or a conversation the bootstrap's BOOTSTRAP_LIMIT
     * cap never reached) and Caroline needs to ask the phone directly.
     * `query` matches against the message body, `address` against the
     * sender/recipient number -- either, both, or neither (= "most
     * recent N messages", a reasonable live sanity check on its own).
     * Same row shape as dumpMessages.
     */
    fun search(query: String?, address: String?, limit: Int): List<Map<String, Any?>> {
        val start = System.currentTimeMillis()
        val out = mutableListOf<Map<String, Any?>>()
        val projection = arrayOf(
            Telephony.Sms.THREAD_ID, Telephony.Sms.ADDRESS, Telephony.Sms.BODY,
            Telephony.Sms.DATE, Telephony.Sms.TYPE, Telephony.Sms.READ,
        )
        val clauses = mutableListOf<String>()
        val args = mutableListOf<String>()
        if (!query.isNullOrBlank()) {
            clauses.add("${Telephony.Sms.BODY} LIKE ?")
            args.add("%$query%")
        }
        if (!address.isNullOrBlank()) {
            clauses.add("${Telephony.Sms.ADDRESS} LIKE ?")
            args.add("%$address%")
        }
        val selection = if (clauses.isEmpty()) null else clauses.joinToString(" AND ")
        context.contentResolver.query(
            Telephony.Sms.CONTENT_URI, projection,
            selection, if (args.isEmpty()) null else args.toTypedArray(),
            "${Telephony.Sms.DATE} DESC",
        )?.use { c -> readRows(c, limit).forEach { out.add(it) } }
        Logger.i("SmsRepository.search(query=$query, address=$address, limit=$limit): ${out.size} row(s) in ${System.currentTimeMillis() - start}ms")
        return out
    }

    private fun readRows(c: Cursor, limit: Int): List<Map<String, Any?>> {
        val out = mutableListOf<Map<String, Any?>>()
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
        return out
    }

    // PduHeaders.FROM / PduHeaders.TO from com.google.android.mms.pdu --
    // not part of the public android.provider.Telephony API, but these
    // integer values are a stable, long-documented part of the MMS content
    // provider's own "addr" table contract (content://mms/<id>/addr),
    // unchanged across Android versions since MMS support was introduced.
    private val MMS_ADDR_TYPE_FROM = 137
    private val MMS_ADDR_TYPE_TO = 151

    /**
     * Real incident (2026-10-05), confirmed live: a message that arrived as
     * MMS (not SMS -- a carrier/Messages-app choice, not something the
     * sender controls, and not the same thing as RCS) was invisible to
     * dumpMessages/search above, which only ever queried content://sms --
     * Caroline kept reporting "no new messages" while the user was reading
     * the message with their own eyes in the SAME Google Messages app.
     * Text-only: an MMS with a photo/video attachment and no text part
     * comes back with a placeholder body, not the actual media -- Caroline
     * has no use for rendering an image over this channel, only for
     * knowing the conversation had something and roughly what/when.
     *
     * content://mms's own DATE column is SECONDS since epoch (NOT
     * milliseconds, unlike content://sms's DATE) -- converted to ms right
     * here so every row this repository returns, SMS or MMS, is in the
     * same unit the backend (companion_sms_store.py) already expects.
     */
    fun dumpMmsMessages(sinceMs: Long?): List<Map<String, Any?>> {
        val start = System.currentTimeMillis()
        val out = mutableListOf<Map<String, Any?>>()
        val projection = arrayOf(Telephony.Mms._ID, Telephony.Mms.THREAD_ID, Telephony.Mms.DATE, Telephony.Mms.MESSAGE_BOX, Telephony.Mms.READ)
        val sinceSec = sinceMs?.let { it / 1000 }
        val selection = if (sinceSec != null) "${Telephony.Mms.DATE} > ?" else null
        val args = if (sinceSec != null) arrayOf(sinceSec.toString()) else null
        context.contentResolver.query(
            Telephony.Mms.CONTENT_URI, projection, selection, args, "${Telephony.Mms.DATE} DESC",
        )?.use { c -> readMmsRows(c).forEach { out.add(it) } }
        Logger.i("SmsRepository.dumpMmsMessages(sinceMs=$sinceMs): ${out.size} row(s) in ${System.currentTimeMillis() - start}ms")
        return out
    }

    /** Live MMS search, same shape/purpose as search() above for SMS -- `address` matches
     * the resolved sender/recipient; MMS has no single indexed body column to LIKE against
     * (text lives in a separate per-part table), so `query` is applied client-side after
     * each message's text is assembled, not pushed into the content-provider selection. */
    fun searchMms(query: String?, address: String?, limit: Int): List<Map<String, Any?>> {
        val start = System.currentTimeMillis()
        val out = mutableListOf<Map<String, Any?>>()
        val projection = arrayOf(Telephony.Mms._ID, Telephony.Mms.THREAD_ID, Telephony.Mms.DATE, Telephony.Mms.MESSAGE_BOX, Telephony.Mms.READ)
        context.contentResolver.query(
            Telephony.Mms.CONTENT_URI, projection, null, null, "${Telephony.Mms.DATE} DESC",
        )?.use { c ->
            for (row in readMmsRows(c)) {
                if (out.size >= limit) break
                val rowAddress = row["address"] as? String
                if (!address.isNullOrBlank() && (rowAddress == null || !rowAddress.contains(address))) continue
                val body = row["body"] as? String ?: ""
                if (!query.isNullOrBlank() && !body.contains(query, ignoreCase = true)) continue
                out.add(row)
            }
        }
        Logger.i("SmsRepository.searchMms(query=$query, address=$address, limit=$limit): ${out.size} row(s) in ${System.currentTimeMillis() - start}ms")
        return out
    }

    private fun readMmsRows(c: Cursor): List<Map<String, Any?>> {
        val out = mutableListOf<Map<String, Any?>>()
        val idIdx = c.getColumnIndexOrThrow(Telephony.Mms._ID)
        val threadIdx = c.getColumnIndexOrThrow(Telephony.Mms.THREAD_ID)
        val dateIdx = c.getColumnIndexOrThrow(Telephony.Mms.DATE)
        val boxIdx = c.getColumnIndexOrThrow(Telephony.Mms.MESSAGE_BOX)
        val readIdx = c.getColumnIndexOrThrow(Telephony.Mms.READ)
        while (c.moveToNext()) {
            val id = c.getLong(idIdx)
            val type = if (c.getInt(boxIdx) == Telephony.Mms.MESSAGE_BOX_INBOX) "inbox" else "sent"
            out.add(
                mapOf(
                    "threadId" to c.getLong(threadIdx).toString(),
                    "address" to getMmsAddress(id, type),
                    "body" to (getMmsTextBody(id) ?: "[MMS with no text part -- likely a photo/video attachment]"),
                    "date" to c.getLong(dateIdx) * 1000L,
                    "type" to type,
                    "read" to (c.getInt(readIdx) != 0),
                ),
            )
        }
        return out
    }

    /** The FROM address for an inbox MMS, or the first TO address for a sent one -- an MMS
     * can have multiple recipients (group thread); this returns one representative address,
     * same simplification dumpMessages/search already make for SMS (a single ADDRESS column). */
    private fun getMmsAddress(mmsId: Long, type: String): String? {
        val wantType = if (type == "inbox") MMS_ADDR_TYPE_FROM else MMS_ADDR_TYPE_TO
        val addrUri = Uri.parse("content://mms/$mmsId/addr")
        return context.contentResolver.query(addrUri, arrayOf("address", "type"), null, null, null)?.use { c ->
            val addrIdx = c.getColumnIndexOrThrow("address")
            val typeIdx = c.getColumnIndexOrThrow("type")
            var fallback: String? = null
            while (c.moveToNext()) {
                val addr = c.getString(addrIdx)
                if (fallback == null) fallback = addr
                if (c.getInt(typeIdx) == wantType) return@use addr
            }
            fallback
        }
    }

    /** Concatenates every text/plain part's body -- some devices store the text inline in
     * the "text" column, others only as a file under content://mms/part/<id>, hence the
     * inline-then-file fallback. Returns null (not "") when there's genuinely no text part,
     * so callers can tell "an empty text message" apart from "no text at all" if it matters. */
    private fun getMmsTextBody(mmsId: Long): String? {
        val partUri = Telephony.Mms.Part.CONTENT_URI
        val projection = arrayOf(Telephony.Mms.Part._ID, Telephony.Mms.Part.CONTENT_TYPE, Telephony.Mms.Part.TEXT)
        val selection = "${Telephony.Mms.Part.MSG_ID} = ?"
        val args = arrayOf(mmsId.toString())
        val parts = mutableListOf<String>()
        context.contentResolver.query(partUri, projection, selection, args, null)?.use { c ->
            val idIdx = c.getColumnIndexOrThrow(Telephony.Mms.Part._ID)
            val ctIdx = c.getColumnIndexOrThrow(Telephony.Mms.Part.CONTENT_TYPE)
            val textIdx = c.getColumnIndexOrThrow(Telephony.Mms.Part.TEXT)
            while (c.moveToNext()) {
                if (c.getString(ctIdx) != "text/plain") continue
                val inlineText = c.getString(textIdx)
                if (!inlineText.isNullOrEmpty()) {
                    parts.add(inlineText)
                    continue
                }
                val partId = c.getLong(idIdx)
                try {
                    context.contentResolver.openInputStream(Uri.withAppendedPath(Telephony.Mms.Part.CONTENT_URI, partId.toString()))
                        ?.use { stream -> parts.add(stream.bufferedReader().readText()) }
                } catch (exc: Exception) {
                    Logger.w("SmsRepository.getMmsTextBody: failed reading part file for partId=$partId: ${exc.message}")
                }
            }
        }
        return if (parts.isEmpty()) null else parts.joinToString("\n")
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
        val sendStartedAt = System.currentTimeMillis()
        val smsManager = if (Build.VERSION.SDK_INT >= 31) context.getSystemService(SmsManager::class.java)
        else @Suppress("DEPRECATION") SmsManager.getDefault()
        val parts = smsManager.divideMessage(text)
        Logger.i("SmsRepository.send: to=$to textLen=${text.length} parts=${parts.size}")
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
                        Logger.i("SmsRepository.send: part broadcast received, resultCode=$resultCode ok=$ok remainingBefore=$remaining")
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
                cont.invokeOnCancellation { Logger.w("SmsRepository.send: coroutine cancelled, unregistering receiver"); try { context.unregisterReceiver(receiver) } catch (_: Exception) {} }
                try {
                    smsManager.sendMultipartTextMessage(to, null, parts, sentIntents, null)
                    Logger.i("SmsRepository.send: sendMultipartTextMessage call returned (does not mean delivered -- awaiting broadcast)")
                } catch (exc: Exception) {
                    Logger.e("SmsManager.sendMultipartTextMessage threw", exc)
                    try { context.unregisterReceiver(receiver) } catch (_: Exception) {}
                    if (cont.isActive) cont.resume(Result.failure(exc))
                }
            }
        }
        if (outcome != null) {
            Logger.i("SmsRepository.send: resolved via broadcast in ${System.currentTimeMillis() - sendStartedAt}ms, outcome=$outcome")
            return outcome
        }
        Logger.w("SmsRepository.send: broadcast confirmation timed out after ${System.currentTimeMillis() - sendStartedAt}ms -- checking content://sms directly")
        // Bug fix (2026-09-30), confirmed live: a real incident sent the same
        // message to a real contact 3 times, because this broadcast-based
        // confirmation timed out all 3 times despite every send actually
        // reaching the phone. SmsManager's SENT PendingIntent fires via a
        // dynamically-registered receiver, which is not reliably delivered
        // within any fixed window on every device/ROM (Doze, aggressive
        // battery management, a slow radio/carrier round trip) -- a timeout
        // here does NOT mean the send failed, so reporting it as a plain
        // error (which the caller reasonably treats as "safe to retry") is
        // itself the bug. Before giving up, check the actual source of
        // truth -- content://sms -- for a sent row matching this address/
        // body that postdates when this call started.
        val actuallySent = wasActuallySent(to, text, sendStartedAt)
        Logger.i("SmsRepository.send: content://sms self-check result=$actuallySent")
        return if (actuallySent) Result.success(Unit)
        else Result.failure(Exception("Timed out waiting for the SMS radio to confirm the send."))
    }

    private fun wasActuallySent(to: String, text: String, sentAfter: Long): Boolean {
        val digits = to.filter { it.isDigit() }
        val suffix = if (digits.length > 10) digits.takeLast(10) else digits
        if (suffix.isEmpty()) {
            Logger.w("SmsRepository.wasActuallySent: 'to' had no usable digits ($to) -- cannot self-check")
            return false
        }
        val projection = arrayOf(Telephony.Sms.ADDRESS, Telephony.Sms.BODY, Telephony.Sms.DATE, Telephony.Sms.TYPE)
        val selection = "${Telephony.Sms.TYPE} = ? AND ${Telephony.Sms.ADDRESS} LIKE ? AND ${Telephony.Sms.BODY} = ? AND ${Telephony.Sms.DATE} > ?"
        val args = arrayOf(Telephony.Sms.MESSAGE_TYPE_SENT.toString(), "%$suffix", text, sentAfter.toString())
        val found = context.contentResolver.query(Telephony.Sms.CONTENT_URI, projection, selection, args, null)
            ?.use { it.moveToFirst() } ?: false
        Logger.i("SmsRepository.wasActuallySent(suffix=$suffix, sentAfter=$sentAfter): found=$found")
        return found
    }
}

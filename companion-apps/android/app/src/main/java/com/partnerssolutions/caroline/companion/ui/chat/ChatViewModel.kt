package com.partnerssolutions.caroline.companion.ui.chat

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.partnerssolutions.caroline.companion.data.model.Attachment
import com.partnerssolutions.caroline.companion.data.model.ChannelStatus
import com.partnerssolutions.caroline.companion.data.model.ChatMessage
import com.partnerssolutions.caroline.companion.data.model.TabStatus
import com.partnerssolutions.caroline.companion.data.remote.CamerlengoRepository
import com.partnerssolutions.caroline.companion.util.Logger
import com.partnerssolutions.caroline.companion.util.TextFiltering
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.util.UUID

// Matches the backend's own INBOX_LOOP_INTERVAL_S (companion_api.py) --
// no point polling faster than the backend could ever produce a new
// synced entry. Lowered 10s -> 3s per explicit instruction (2026-09-15),
// matching the backend's own interval drop.
private const val AUTO_REFRESH_INTERVAL_MS = 3_000L

/**
 * A file picked on the phone, not yet sent -- read from the picker's
 * content:// Uri (see ChatScreen's attach button) and base64-encoded
 * client-side, same wire shape companion_api.py's _drain_inbox now expects
 * ("name"/"mimeType"/"dataBase64") and chat_session.py's own
 * _attachment_to_blocks already knows how to turn into real image/document
 * content blocks -- per explicit instruction (2026-09-15), the phone->
 * desktop direction carries real bytes exactly like desktop->phone does.
 */
data class PendingAttachment(
    val name: String,
    val mimeType: String,
    val dataBase64: String,
)

/**
 * Reads tabs/<tabId>/history/<index> (companion_api.py's _sync_history --
 * one Camerlengo key per message). Read-only remote fetch for now -- no
 * local Room cache, so no true offline support. Polls automatically while
 * this tab is open (see AUTO_REFRESH_INTERVAL_MS) so Caroline's replies
 * show up without the user having to leave and re-open the tab.
 */
class ChatViewModel(
    private val tabId: String,
    private val repository: CamerlengoRepository = CamerlengoRepository(),
) : ViewModel() {
    var messages by mutableStateOf<List<ChatMessage>>(emptyList())
        private set
    var isLoading by mutableStateOf(true)
        private set
    var error by mutableStateOf<String?>(null)
        private set
    var isSending by mutableStateOf(false)
        private set
    var sendError by mutableStateOf<String?>(null)
        private set
    // Mirrors the desktop app's own lamp 1 + status-bar text (this tab's
    // own tabs/<tabId>/status) and lamp 2 (the global channel_status) --
    // per explicit instruction (2026-09-15), the phone should show
    // exactly what the desktop shows. Defaults are the "everything's
    // fine, nothing known yet" state, same spirit as the desktop's own
    // initial carolineStatus="ready" before the first real status lands.
    var tabStatus by mutableStateOf(TabStatus.READY)
        private set
    var channelStatus by mutableStateOf(ChannelStatus.UNKNOWN)
        private set

    // Highest real (synced) index seen, so an optimistic local echo (see
    // sendMessage) can be told apart from and cleanly dropped once the
    // real synced copy of the same message arrives.
    private var optimisticMessages: List<ChatMessage> = emptyList()

    init {
        viewModelScope.launch {
            while (isActive) {
                refreshOnce()
                delay(AUTO_REFRESH_INTERVAL_MS)
            }
        }
    }

    fun refresh() {
        viewModelScope.launch { refreshOnce() }
    }

    private suspend fun refreshOnce() {
        try {
            val raw = repository.getAllMine("tabs/$tabId/history")
            val synced = raw.entries
                .mapNotNull { (key, value) ->
                    val index = (key as? String)?.toIntOrNull() ?: return@mapNotNull null
                    val entry = value as? Map<*, *> ?: return@mapNotNull null
                    val rawText = entry["text"] as? String ?: ""
                    // Client-side only, per explicit instruction (2026-09-11)
                    // -- the backend hands over the SAME raw text its own
                    // model-facing session sees (a "[Sent: ...]" stamp, and
                    // occasionally a whole synthetic/internal turn); this
                    // app decides what a human should actually see, same as
                    // the desktop's own chat.js does for its live rendering.
                    if (TextFiltering.isSyntheticText(rawText)) return@mapNotNull null
                    @Suppress("UNCHECKED_CAST")
                    val rawAttachments = entry["attachments"] as? List<Map<*, *>>
                    val attachments = rawAttachments?.mapNotNull { a ->
                        val name = a["name"] as? String ?: return@mapNotNull null
                        Attachment(
                            name = name,
                            mimeType = a["mimeType"] as? String,
                            dataBase64 = a["dataBase64"] as? String,
                            tooLarge = a["tooLarge"] as? Boolean ?: false,
                        )
                    } ?: emptyList()
                    ChatMessage(
                        index = index,
                        role = entry["role"] as? String ?: "user",
                        text = TextFiltering.stripStamp(rawText),
                        ts = (entry["ts"] as? Double)?.toLong() ?: 0L,
                        attachments = attachments,
                    )
                }
                .sortedBy { it.index }
            // Drop any optimistic echo whose text now has a real synced
            // counterpart -- the backend round-trip caught up.
            val syncedTexts = synced.map { it.text }.toSet()
            optimisticMessages = optimisticMessages.filterNot { it.text in syncedTexts }
            messages = synced + optimisticMessages
            refreshStatus()
            error = null
        } catch (exc: Exception) {
            Logger.e("ChatViewModel refresh failed for tab $tabId", exc)
            error = exc.message ?: "Couldn't load messages."
        } finally {
            isLoading = false
        }
    }

    /**
     * Pulls this tab's own lamp-1/status-bar source plus the one global
     * channel_status (lamp 2) -- separate try/catch from the history fetch
     * above so a status hiccup never blanks out already-loaded messages,
     * and vice versa. Best-effort: silently keeps the last-known value on
     * failure rather than surfacing a second error UI on top of the
     * message list's own.
     */
    private suspend fun refreshStatus() {
        try {
            (repository.getMine("tabs/$tabId/status") as? Map<*, *>)?.let { raw ->
                tabStatus = TabStatus(
                    state = raw["state"] as? String ?: "ready",
                    reason = raw["reason"] as? String ?: "",
                )
            }
        } catch (exc: Exception) {
            Logger.e("tab status refresh failed for tab $tabId", exc)
        }
        try {
            (repository.getMine("channel_status") as? Map<*, *>)?.let { raw ->
                channelStatus = ChannelStatus(
                    enabled = raw["enabled"] as? Boolean ?: false,
                    tick_count = (raw["tick_count"] as? Double)?.toInt() ?: 0,
                    last_tick_at_iso = raw["last_tick_at_iso"] as? String,
                    last_tick_outcome = raw["last_tick_outcome"] as? String,
                    caroline_email = raw["caroline_email"] as? String,
                )
            }
        } catch (exc: Exception) {
            Logger.e("channel status refresh failed", exc)
        }
    }

    /**
     * Writes a phone-originated message to tabs/<tabId>/inbox/<id> --
     * companion_api.py's _drain_inbox picks it up and injects it into the
     * tab's live desktop session as if typed there. Echoed into the list
     * immediately (optimistic) since the real synced copy won't appear
     * for a while (inbox drain -> Caroline replies -> next history sync,
     * up to ~2x AUTO_REFRESH_INTERVAL_MS later) -- otherwise sending felt
     * like it did nothing.
     */
    fun sendMessage(text: String, attachment: PendingAttachment?, onSent: () -> Unit) {
        val trimmed = text.trim()
        if (trimmed.isEmpty() && attachment == null) return
        isSending = true
        sendError = null
        viewModelScope.launch {
            try {
                val id = UUID.randomUUID().toString().replace("-", "")
                val payload = buildMap<String, Any> {
                    put("text", trimmed)
                    put("ts", System.currentTimeMillis())
                    if (attachment != null) {
                        put(
                            "attachments",
                            listOf(mapOf("name" to attachment.name, "mimeType" to attachment.mimeType, "dataBase64" to attachment.dataBase64)),
                        )
                    }
                }
                repository.setMine("tabs/$tabId/inbox/$id", payload)
                Logger.i("sent message into tab $tabId inbox ($id), attachment=${attachment != null}")
                optimisticMessages = optimisticMessages + ChatMessage(
                    index = Int.MAX_VALUE - optimisticMessages.size,
                    role = "user",
                    text = trimmed,
                    ts = System.currentTimeMillis(),
                    attachments = attachment?.let {
                        listOf(Attachment(name = it.name, mimeType = it.mimeType, dataBase64 = it.dataBase64))
                    } ?: emptyList(),
                )
                messages = messages + optimisticMessages.last()
                onSent()
            } catch (exc: Exception) {
                Logger.e("send failed for tab $tabId", exc)
                sendError = exc.message ?: "Send failed."
            } finally {
                isSending = false
            }
        }
    }
}

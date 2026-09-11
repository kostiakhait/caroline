package com.partnerssolutions.caroline.companion.ui.chat

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.partnerssolutions.caroline.companion.data.model.ChatMessage
import com.partnerssolutions.caroline.companion.data.remote.CamerlengoRepository
import com.partnerssolutions.caroline.companion.util.Logger
import com.partnerssolutions.caroline.companion.util.TextFiltering
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.util.UUID

// Matches the backend's own INBOX_LOOP_INTERVAL_S (companion_api.py) --
// no point polling faster than the backend could ever produce a new
// synced entry.
private const val AUTO_REFRESH_INTERVAL_MS = 10_000L

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
                    ChatMessage(
                        index = index,
                        role = entry["role"] as? String ?: "user",
                        text = TextFiltering.stripStamp(rawText),
                        ts = (entry["ts"] as? Double)?.toLong() ?: 0L,
                    )
                }
                .sortedBy { it.index }
            // Drop any optimistic echo whose text now has a real synced
            // counterpart -- the backend round-trip caught up.
            val syncedTexts = synced.map { it.text }.toSet()
            optimisticMessages = optimisticMessages.filterNot { it.text in syncedTexts }
            messages = synced + optimisticMessages
            error = null
        } catch (exc: Exception) {
            Logger.e("ChatViewModel refresh failed for tab $tabId", exc)
            error = exc.message ?: "Couldn't load messages."
        } finally {
            isLoading = false
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
    fun sendMessage(text: String, onSent: () -> Unit) {
        val trimmed = text.trim()
        if (trimmed.isEmpty()) return
        isSending = true
        sendError = null
        viewModelScope.launch {
            try {
                val id = UUID.randomUUID().toString().replace("-", "")
                repository.setMine("tabs/$tabId/inbox/$id", mapOf("text" to trimmed, "ts" to System.currentTimeMillis()))
                Logger.i("sent message into tab $tabId inbox ($id)")
                optimisticMessages = optimisticMessages + ChatMessage(
                    index = Int.MAX_VALUE - optimisticMessages.size,
                    role = "user",
                    text = trimmed,
                    ts = System.currentTimeMillis(),
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

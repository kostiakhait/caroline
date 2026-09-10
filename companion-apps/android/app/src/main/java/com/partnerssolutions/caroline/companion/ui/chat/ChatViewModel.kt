package com.partnerssolutions.caroline.companion.ui.chat

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.partnerssolutions.caroline.companion.data.model.ChatMessage
import com.partnerssolutions.caroline.companion.data.remote.CamerlengoRepository
import com.partnerssolutions.caroline.companion.data.remote.SessionHolder
import kotlinx.coroutines.launch

/**
 * Reads tabs/<tabId>/history/<index> (companion_api.py's _sync_history --
 * one Camerlengo key per message). Read-only for now: no local Room cache
 * (so no true offline support yet -- the confirmed architecture direction,
 * just not built), and no way to actually SEND a message into
 * tabs/<tabId>/inbox yet either. Both are the natural next pieces, not
 * attempted in this skeleton pass.
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

    init {
        refresh()
    }

    fun refresh() {
        val session = SessionHolder.session ?: run {
            error = "Not logged in."
            isLoading = false
            return
        }
        isLoading = true
        error = null
        viewModelScope.launch {
            try {
                val raw = repository.getAllMine(session, "tabs/$tabId/history")
                messages = raw.entries
                    .mapNotNull { (key, value) ->
                        val index = (key as? String)?.toIntOrNull() ?: return@mapNotNull null
                        val entry = value as? Map<*, *> ?: return@mapNotNull null
                        ChatMessage(
                            index = index,
                            role = entry["role"] as? String ?: "user",
                            text = entry["text"] as? String ?: "",
                            ts = (entry["ts"] as? Double)?.toLong() ?: 0L,
                        )
                    }
                    .sortedBy { it.index }
            } catch (exc: Exception) {
                error = exc.message ?: "Couldn't load messages."
            } finally {
                isLoading = false
            }
        }
    }
}

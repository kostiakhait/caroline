package com.partnerssolutions.caroline.companion.ui.tabs

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.partnerssolutions.caroline.companion.data.model.TabInfo
import com.partnerssolutions.caroline.companion.data.remote.CamerlengoRepository
import com.partnerssolutions.caroline.companion.data.remote.SessionHolder
import kotlinx.coroutines.launch

/**
 * Reads the LIVE tab directory (main repo's main.py "tab_list_set" control
 * op -> Camerlengo `tabs_list`) -- never a hardcoded tab set. Each user's
 * desktop Caroline has its own, arbitrary tabs (created/renamed/closed
 * freely), so this is the only correct source.
 *
 * Skeleton note: one-shot fetch, no periodic refresh/WorkManager sync yet
 * -- the confirmed v1 mechanism is a foreground-service poll loop, not
 * built here yet (see the plan's Phase 3 notes).
 */
class TabsViewModel(private val repository: CamerlengoRepository = CamerlengoRepository()) : ViewModel() {
    var tabs by mutableStateOf<List<TabInfo>>(emptyList())
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
                @Suppress("UNCHECKED_CAST")
                val raw = repository.getMine(session, "tabs_list") as? List<Map<String, Any?>>
                tabs = raw?.mapNotNull { entry ->
                    val id = entry["id"] as? String ?: return@mapNotNull null
                    val name = entry["name"] as? String ?: return@mapNotNull null
                    TabInfo(id, name)
                } ?: emptyList()
            } catch (exc: Exception) {
                error = exc.message ?: "Couldn't load tabs."
            } finally {
                isLoading = false
            }
        }
    }
}

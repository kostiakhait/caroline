package com.partnerssolutions.caroline.companion.ui.tabs

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.partnerssolutions.caroline.companion.data.model.TabInfo
import com.partnerssolutions.caroline.companion.data.remote.CamerlengoRepository
import com.partnerssolutions.caroline.companion.util.Logger
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

// Matches the backend's own INBOX_LOOP_INTERVAL_S (companion_api.py) --
// no point polling the tab directory faster than the backend could ever
// update it.
private const val AUTO_REFRESH_INTERVAL_MS = 10_000L

/**
 * Reads the LIVE tab directory (main repo's main.py "tab_list_set" control
 * op -> Camerlengo `tabs_list`) -- never a hardcoded tab set. Each user's
 * desktop Caroline has its own, arbitrary tabs (created/renamed/closed
 * freely), so this is the only correct source. Polls automatically while
 * this ViewModel is alive so a tab created on the desktop after the app
 * opened still shows up without a manual restart.
 */
class TabsViewModel(private val repository: CamerlengoRepository = CamerlengoRepository()) : ViewModel() {
    var tabs by mutableStateOf<List<TabInfo>>(emptyList())
        private set
    var isLoading by mutableStateOf(true)
        private set
    var error by mutableStateOf<String?>(null)
        private set

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
            @Suppress("UNCHECKED_CAST")
            val raw = repository.getMine("tabs_list") as? List<Map<String, Any?>>
            tabs = raw?.mapNotNull { entry ->
                val id = entry["id"] as? String ?: return@mapNotNull null
                val name = entry["name"] as? String ?: return@mapNotNull null
                TabInfo(id, name)
            } ?: emptyList()
            error = null
        } catch (exc: Exception) {
            Logger.e("TabsViewModel refresh failed", exc)
            error = exc.message ?: "Couldn't load tabs."
        } finally {
            isLoading = false
        }
    }
}

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
// update it. Lowered 10s -> 3s per explicit instruction (2026-09-15),
// matching the backend's own interval drop.
private const val AUTO_REFRESH_INTERVAL_MS = 3_000L

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

    // Per explicit instruction (2026-09-26), after a real glitch: the very
    // FIRST poll on cold start can lose a one-off race against the process's
    // own network/DNS warmup (UnknownHostException, message like "Unable to
    // resolve host ...") and fail on its own -- before the 2026-09-16 fix
    // below even applies, since tabs is still empty at that point. That
    // surfaced a scary full-screen error for ~3s, immediately followed by
    // normal content once the next poll succeeded. One early failure while
    // tabs is still empty is now tolerated silently (falls through to the
    // plain "No tabs yet" branch instead) -- only two in a row surfaces the
    // real error screen, same "don't tear down the UI over one flaky tick"
    // reasoning as below, just extended to the empty-list case too.
    private var consecutiveEmptyLoadFailures = 0

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
            consecutiveEmptyLoadFailures = 0
        } catch (exc: Exception) {
            Logger.e("TabsViewModel refresh failed", exc)
            // Bug fix (2026-09-16), confirmed live: this used to
            // unconditionally set `error` on ANY failed poll -- and
            // CompanionTabsScreen renders a non-null error as a full-screen
            // replacement for the tabs+ChatScreen branch, tearing ChatScreen
            // completely out of composition on a single transient hiccup
            // (one flaky network tick out of many). That silently destroyed
            // every bit of ChatScreen's own local state, including an
            // in-flight attachment-picker registration -- confirmed live:
            // picking a file, then a routine 3s poll failing while the
            // system picker was still open, orphaned the pick entirely with
            // no error shown to the user, because the NEW ChatScreen
            // instance that replaced the old one never called
            // attachmentPicker.launch() itself. Once we have a real tabs
            // list, a refresh failure is background noise -- log it, keep
            // showing the last known list (same "best-effort, keep last
            // value" pattern ChatViewModel.refreshStatus() already uses).
            // Only surface the error screen when there's nothing to fall
            // back to yet (the very first load).
            if (tabs.isEmpty()) {
                consecutiveEmptyLoadFailures++
                if (consecutiveEmptyLoadFailures >= 2) {
                    error = exc.message ?: "Couldn't load tabs."
                }
            }
        } finally {
            isLoading = false
        }
    }
}

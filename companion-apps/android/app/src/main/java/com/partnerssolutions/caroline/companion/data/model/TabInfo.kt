package com.partnerssolutions.caroline.companion.data.model

/**
 * One entry from Caroline's backend-published `tabs_list` (see
 * MainWindow.xaml.cs's SyncTabListToBackend() and main.py's "tab_list_set"
 * control op, both in the main caroline repo). The whole point of this
 * shape existing is that neither the SET of tabs nor their [name]s are
 * ever hardcoded here -- always read live from the backend.
 */
data class TabInfo(
    val id: String,
    val name: String,
)

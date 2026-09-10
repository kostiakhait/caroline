package com.partnerssolutions.caroline.companion.ui.tabs

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.ScrollableTabRow
import androidx.compose.material3.Tab
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.partnerssolutions.caroline.companion.data.remote.SessionHolder
import com.partnerssolutions.caroline.companion.ui.chat.ChatScreen
import com.partnerssolutions.caroline.companion.util.Logger

/**
 * A persistent top tab bar -- matching the DESKTOP app's own tab strip
 * ("Основной диалог | Дополнительные задачи | ... | +"), NOT Ratatosk's
 * own list-then-push-navigation (explicit correction, 2026-09-10: this is
 * the one place Caroline's companion app deliberately diverges from the
 * Ratatosk UI it's otherwise modeled on). Tab set/names come from
 * TabsViewModel -- never hardcoded here.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CompanionTabsScreen(onLogout: () -> Unit, viewModel: TabsViewModel = viewModel()) {
    var selectedIndex by remember { mutableIntStateOf(0) }
    var menuOpen by remember { mutableStateOf(false) }
    val context = LocalContext.current

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Caroline") },
                actions = {
                    IconButton(onClick = { menuOpen = true }) {
                        Icon(Icons.Filled.MoreVert, contentDescription = "Menu")
                    }
                    DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                        DropdownMenuItem(
                            text = { Text("Share logs") },
                            onClick = {
                                menuOpen = false
                                Logger.shareLogs(context)
                            },
                        )
                        DropdownMenuItem(
                            text = { Text("Log out") },
                            onClick = {
                                menuOpen = false
                                Logger.i("user logged out")
                                SessionHolder.clear()
                                onLogout()
                            },
                        )
                    }
                },
            )
        },
    ) { padding ->
        Box(modifier = Modifier.padding(padding).fillMaxSize()) {
            when {
                viewModel.isLoading -> CircularProgressIndicator(modifier = Modifier.align(Alignment.Center))
                viewModel.error != null -> Text(
                    viewModel.error ?: "",
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.align(Alignment.Center).padding(24.dp),
                )
                viewModel.tabs.isEmpty() -> Text(
                    "No tabs yet -- open Caroline on the desktop first.",
                    modifier = Modifier.align(Alignment.Center).padding(24.dp),
                )
                else -> {
                    val tabs = viewModel.tabs
                    val safeIndex = selectedIndex.coerceIn(0, tabs.lastIndex)
                    Column(modifier = Modifier.fillMaxSize()) {
                        ScrollableTabRow(selectedTabIndex = safeIndex) {
                            tabs.forEachIndexed { index, tab ->
                                Tab(
                                    selected = index == safeIndex,
                                    onClick = { selectedIndex = index },
                                    text = { Text(tab.name.ifBlank { "Tab ${tab.id}" }) },
                                )
                            }
                        }
                        ChatScreen(tabId = tabs[safeIndex].id)
                    }
                }
            }
        }
    }
}

package com.partnerssolutions.caroline.companion.ui.tabs

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.ScrollableTabRow
import androidx.compose.material3.Tab
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.partnerssolutions.caroline.companion.ui.chat.ChatScreen
import androidx.lifecycle.viewmodel.compose.viewModel

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
fun CompanionTabsScreen(viewModel: TabsViewModel = viewModel()) {
    var selectedIndex by remember { mutableIntStateOf(0) }

    Scaffold(
        topBar = {
            TopAppBar(title = { Text("Caroline") })
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
                                    text = { Text(tab.name) },
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

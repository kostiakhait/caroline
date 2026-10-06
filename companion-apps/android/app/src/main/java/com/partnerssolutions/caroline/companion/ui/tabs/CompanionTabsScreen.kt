package com.partnerssolutions.caroline.companion.ui.tabs

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material3.AlertDialog
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
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.viewmodel.compose.viewModel
import com.partnerssolutions.caroline.companion.BuildConfig
import com.partnerssolutions.caroline.companion.data.companion.COMPANION_REQUIRED_PERMISSIONS
import com.partnerssolutions.caroline.companion.data.companion.CompanionPrefs
import com.partnerssolutions.caroline.companion.data.companion.activateCompanionService
import com.partnerssolutions.caroline.companion.data.companion.companionPermissionsGranted
import com.partnerssolutions.caroline.companion.data.companion.restartCompanionServiceIfNeeded
import com.partnerssolutions.caroline.companion.data.remote.CamerlengoRepository
import com.partnerssolutions.caroline.companion.ui.chat.ChatScreen
import com.partnerssolutions.caroline.companion.update.ACTION_UPDATE_READY
import com.partnerssolutions.caroline.companion.update.CheckResult
import com.partnerssolutions.caroline.companion.update.UpdateChecker
import com.partnerssolutions.caroline.companion.util.Logger
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

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
fun CompanionTabsScreen(onLogout: () -> Unit, onOpenCompanionSetup: () -> Unit, viewModel: TabsViewModel = viewModel()) {
    var selectedIndex by remember { mutableIntStateOf(0) }
    var menuOpen by remember { mutableStateOf(false) }
    var showAboutDialog by remember { mutableStateOf(false) }
    val context = LocalContext.current

    // OTA self-update (2026-10-01), same pattern as ShortNerdCat's own
    // MainActivity: starts true if UpdateChecker (running since
    // CompanionApplication.onCreate) already had a verified APK sitting on
    // disk before this screen was ever composed, then flips true live via
    // ACTION_UPDATE_READY for an update that finishes downloading while
    // the app is open.
    var updateReady by remember { mutableStateOf(UpdateChecker.readyVersion(context) != null) }
    var checkingForUpdate by remember { mutableStateOf(false) }
    val coroutineScope = rememberCoroutineScope()
    DisposableEffect(context) {
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(ctx: Context, intent: Intent) {
                if (intent.action == ACTION_UPDATE_READY) {
                    Logger.i("CompanionTabsScreen: received ACTION_UPDATE_READY")
                    updateReady = true
                }
            }
        }
        val filter = IntentFilter(ACTION_UPDATE_READY)
        ContextCompat.registerReceiver(context, receiver, filter, ContextCompat.RECEIVER_NOT_EXPORTED)
        onDispose { context.unregisterReceiver(receiver) }
    }

    // Per explicit instruction (2026-09-27), reversing the 2026-09-24
    // opt-in decision: the SMS/contacts companion now activates itself the
    // first time this screen is reached, instead of waiting for the user
    // to find CompanionSetupScreen in the overflow menu and flip a switch.
    // The permission dialogs below are Android's own -- unavoidable, not
    // an extra app-level menu -- and a decline just leaves the feature off
    // exactly as before (CompanionSetupScreen still lets the user turn it
    // on/off, or correct the number, at any time).
    val permissionLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { results ->
        Logger.i("CompanionTabsScreen: permission results $results")
        if (results.values.all { it }) {
            activateCompanionService(context, CompanionPrefs.bestEffortDetectedNumber(context) ?: CompanionPrefs.phoneNumber ?: "")
        }
    }
    LaunchedEffect(Unit) {
        Logger.i("CompanionTabsScreen: initial LaunchedEffect (cold start)")
        // Re-detect on every launch regardless of whether the feature was
        // already on (see CompanionPrefs.bestEffortDetectedNumber's own
        // doc comment) -- a null result leaves any existing saved/manual
        // number alone.
        CompanionPrefs.bestEffortDetectedNumber(context)?.let { CompanionPrefs.phoneNumber = it }
        if (!CompanionPrefs.enabled) {
            if (companionPermissionsGranted(context)) {
                activateCompanionService(context, CompanionPrefs.phoneNumber ?: "")
            } else {
                permissionLauncher.launch(COMPANION_REQUIRED_PERMISSIONS)
            }
        } else {
            // Per explicit instruction (2026-09-30): CompanionPrefs.enabled
            // staying true doesn't mean the service is actually still
            // alive (see restartCompanionServiceIfNeeded's own doc
            // comment) -- cold start is one of the two times this needs
            // checking, the other being every resume (below).
            restartCompanionServiceIfNeeded(context)
        }
    }

    // Per explicit instruction (2026-09-30): "перезапускать... каждый раз
    // при заходе в Кэролайн" -- LaunchedEffect(Unit) above only ever runs
    // once per composition (effectively once per cold start), so bringing
    // the app back to the foreground after backgrounding it (no process
    // death, so no recomposition) never re-ran that check. ON_RESUME fires
    // every single time the user returns to this screen, cold start or not.
    val lifecycleOwner = LocalLifecycleOwner.current
    val currentContext = rememberUpdatedState(context)
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) {
                restartCompanionServiceIfNeeded(currentContext.value)
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Caroline") },
                actions = {
                    IconButton(onClick = { menuOpen = true }) {
                        Icon(Icons.Filled.MoreVert, contentDescription = "Menu")
                    }
                    DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                        if (updateReady) {
                            val readyVersion = UpdateChecker.readyVersion(context)
                            DropdownMenuItem(
                                text = { Text("Update ready" + (readyVersion?.let { " ($it)" } ?: "")) },
                                onClick = {
                                    menuOpen = false
                                    Logger.i("CompanionTabsScreen: 'Update ready' tapped (version=$readyVersion)")
                                    val apk = UpdateChecker.readyApkFile(context) ?: run {
                                        Logger.w("CompanionTabsScreen: readyApkFile returned null despite updateReady=true")
                                        return@DropdownMenuItem
                                    }
                                    val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", apk)
                                    val intent = Intent(Intent.ACTION_VIEW).apply {
                                        setDataAndType(uri, "application/vnd.android.package-archive")
                                        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                                        addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                                    }
                                    context.startActivity(intent)
                                },
                            )
                        }
                        DropdownMenuItem(
                            text = { Text(if (checkingForUpdate) "Checking for updates..." else "Check for updates...") },
                            enabled = !checkingForUpdate,
                            onClick = {
                                menuOpen = false
                                checkingForUpdate = true
                                Logger.i("CompanionTabsScreen: 'Check for updates...' tapped")
                                coroutineScope.launch {
                                    val result = withContext(Dispatchers.IO) { UpdateChecker(context).checkNow() }
                                    checkingForUpdate = false
                                    when (result) {
                                        is CheckResult.UpToDate ->
                                            Toast.makeText(context, "You're on the latest version (${result.currentVersion}).", Toast.LENGTH_SHORT).show()
                                        is CheckResult.ReadyToInstall -> {
                                            updateReady = true
                                            Toast.makeText(context, "Update ${result.version} downloaded -- see \"Update ready\" in this menu.", Toast.LENGTH_LONG).show()
                                        }
                                        is CheckResult.Error ->
                                            Toast.makeText(context, result.message, Toast.LENGTH_LONG).show()
                                    }
                                }
                            },
                        )
                        DropdownMenuItem(
                            text = { Text("Phone companion (SMS/contacts)") },
                            onClick = {
                                menuOpen = false
                                Logger.i("CompanionTabsScreen: opening companion setup")
                                onOpenCompanionSetup()
                            },
                        )
                        DropdownMenuItem(
                            text = { Text("Share logs") },
                            onClick = {
                                menuOpen = false
                                Logger.i("CompanionTabsScreen: sharing logs")
                                Logger.shareLogs(context)
                            },
                        )
                        DropdownMenuItem(
                            text = { Text("About") },
                            onClick = {
                                menuOpen = false
                                Logger.i("CompanionTabsScreen: opening About dialog")
                                showAboutDialog = true
                            },
                        )
                        DropdownMenuItem(
                            text = { Text("Log out") },
                            onClick = {
                                menuOpen = false
                                Logger.i("user logged out")
                                CamerlengoRepository().logout()
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

    if (showAboutDialog) {
        AlertDialog(
            onDismissRequest = { showAboutDialog = false },
            title = { Text("About") },
            text = { Text("Caroline Companion\nVersion ${BuildConfig.VERSION_NAME} (build ${BuildConfig.VERSION_CODE})") },
            confirmButton = {
                TextButton(onClick = { showAboutDialog = false }) { Text("OK") }
            },
        )
    }
}

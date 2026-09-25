package com.partnerssolutions.caroline.companion.ui.companion

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.Scaffold
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.partnerssolutions.caroline.companion.data.companion.COMPANION_REQUIRED_PERMISSIONS
import com.partnerssolutions.caroline.companion.data.companion.CompanionPrefs
import com.partnerssolutions.caroline.companion.data.companion.companionPermissionsGranted
import com.partnerssolutions.caroline.companion.service.CompanionOpsService

/**
 * Consent + control screen for the SMS/contacts phone companion
 * (CompanionOpsService) -- per explicit instruction (2026-09-24), this is
 * opt-in, not auto-started after login: sending a real SMS is a real-
 * world-consequence action, and the foreground service's persistent
 * notification shouldn't appear without the user having explicitly asked
 * for the feature. Reachable from CompanionTabsScreen's overflow menu.
 *
 * Multi-phone support (explicit instruction, 2026-09-26) added the phone
 * number field below: Caroline distinguishes paired phones BY NUMBER, and
 * Android frequently can't report this phone's own number reliably on its
 * own (see CompanionPrefs.bestEffortDetectedNumber's own doc comment), so
 * the user confirms/corrects it here rather than the feature silently
 * pairing under a blank or wrong number.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CompanionSetupScreen(onBack: () -> Unit) {
    val context = LocalContext.current
    var enabled by remember { mutableStateOf(CompanionPrefs.enabled && companionPermissionsGranted(context)) }
    var phoneNumber by remember {
        mutableStateOf(CompanionPrefs.phoneNumber ?: CompanionPrefs.bestEffortDetectedNumber(context) ?: "")
    }
    val numberValid = phoneNumber.trim().length >= 7

    fun activate() {
        CompanionPrefs.phoneNumber = phoneNumber
        CompanionPrefs.enabled = true
        CompanionOpsService.start(context)
        enabled = true
    }

    val permissionLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { results ->
        if (results.values.all { it }) {
            activate()
        } else {
            // Partial grant is not good enough (sending needs SEND_SMS,
            // lookups need READ_SMS/READ_CONTACTS) -- leave it off rather
            // than start a service that can only half do its job.
            enabled = false
        }
    }

    Scaffold(topBar = { TopAppBar(title = { Text("Phone companion") }) }) { padding ->
        Column(
            modifier = Modifier.fillMaxSize().padding(padding).padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            Text(
                "Lets your desktop Caroline send a real text message from this phone's own number, and read "
                    + "this phone's SMS threads and contacts when you ask her to -- e.g. \"text mom I'm running "
                    + "late\" or \"what's John's number?\". You can pair more than one phone to the same "
                    + "account -- Caroline tells them apart by the number below.",
                style = MaterialTheme.typography.bodyMedium,
            )
            Text(
                "Nothing happens automatically: Caroline only reads or sends when you explicitly ask her to in "
                    + "a chat. This phone stays reachable via a small persistent notification while the feature "
                    + "is on.",
                style = MaterialTheme.typography.bodyMedium,
            )
            OutlinedTextField(
                value = phoneNumber,
                onValueChange = { phoneNumber = it },
                label = { Text("This phone's number") },
                supportingText = {
                    Text(
                        if (numberValid) "Used so Caroline can tell this phone apart from any others you pair."
                        else "Android often can't detect this automatically -- please enter it.",
                    )
                },
                enabled = !enabled,
                modifier = Modifier.fillMaxWidth(),
            )
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Switch(
                    checked = enabled,
                    enabled = enabled || numberValid,
                    onCheckedChange = { turnOn ->
                        if (turnOn) {
                            if (companionPermissionsGranted(context)) {
                                activate()
                            } else {
                                permissionLauncher.launch(COMPANION_REQUIRED_PERMISSIONS)
                            }
                        } else {
                            CompanionPrefs.enabled = false
                            CompanionOpsService.stop(context)
                            enabled = false
                        }
                    },
                )
                Text(if (enabled) "Enabled" else "Disabled", style = MaterialTheme.typography.bodyLarge)
            }
            OutlinedButton(onClick = onBack) { Text("Back") }
        }
    }
}

package com.partnerssolutions.caroline.companion.ui.chat

import android.content.Intent
import android.graphics.BitmapFactory
import android.provider.OpenableColumns
import android.util.Base64
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.AttachFile
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.InsertDriveFile
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.partnerssolutions.caroline.companion.data.model.Attachment
import com.partnerssolutions.caroline.companion.data.model.ChannelStatus
import com.partnerssolutions.caroline.companion.data.model.ChatMessage
import com.partnerssolutions.caroline.companion.data.model.TabStatus
import com.partnerssolutions.caroline.companion.util.Logger
import java.io.File

/**
 * iMessage-style bubbles matching the confirmed Ratatosk-derived
 * direction. Read history over the network + write new messages into
 * tabs/<tabId>/inbox, both carrying real attachment bytes (2026-09-15,
 * "точное соответствие... в обе стороны") -- images render inline, other
 * files open with whatever app the phone has for them. No local Room
 * cache yet (so no true offline support), no reply/edit/delete (those
 * depend on backend capabilities that don't exist yet) -- long-press
 * gives Copy.
 */
@Composable
fun ChatScreen(tabId: String) {
    val factory = viewModelFactory { initializer { ChatViewModel(tabId) } }
    val viewModel: ChatViewModel = viewModel(key = "chat-$tabId", factory = factory)
    val listState = rememberLazyListState()
    val context = LocalContext.current
    var input by remember { mutableStateOf("") }
    var pendingAttachment by remember { mutableStateOf<PendingAttachment?>(null) }

    // Per explicit instruction (2026-09-15): the phone must be able to send
    // real attachment bytes to the desktop too, not just receive them --
    // "точное соответствие... в обе стороны". GetContent hands back a
    // content:// Uri; read its bytes + display name via the resolver (no
    // storage permission needed for a picker-granted Uri), then base64
    // encode for the same wire shape the desktop side already emits.
    val attachmentPicker = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        if (uri == null) return@rememberLauncherForActivityResult
        try {
            val resolver = context.contentResolver
            val mimeType = resolver.getType(uri) ?: "application/octet-stream"
            var name = "attachment"
            resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
                if (cursor.moveToFirst()) {
                    val idx = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    if (idx >= 0) cursor.getString(idx)?.let { name = it }
                }
            }
            val bytes = resolver.openInputStream(uri)?.use { it.readBytes() }
            if (bytes != null) {
                pendingAttachment = PendingAttachment(name, mimeType, Base64.encodeToString(bytes, Base64.NO_WRAP))
            }
        } catch (exc: Exception) {
            Logger.e("failed to read picked attachment", exc)
        }
    }

    // Land at (and follow) the newest message.
    LaunchedEffect(viewModel.messages.size) {
        if (viewModel.messages.isNotEmpty()) {
            listState.scrollToItem(viewModel.messages.lastIndex)
        }
    }

    Column(modifier = Modifier.fillMaxSize()) {
        Box(modifier = Modifier.fillMaxSize().weight(1f)) {
            when {
                viewModel.isLoading -> CircularProgressIndicator(modifier = Modifier.align(Alignment.Center))
                viewModel.error != null -> Text(
                    viewModel.error ?: "",
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.align(Alignment.Center).padding(24.dp),
                )
                viewModel.messages.isEmpty() -> Text(
                    "No messages synced yet.",
                    modifier = Modifier.align(Alignment.Center).padding(24.dp),
                )
                else -> LazyColumn(
                    state = listState,
                    modifier = Modifier
                        .fillMaxSize()
                        .padding(horizontal = 12.dp)
                        .verticalScrollbar(listState),
                    verticalArrangement = Arrangement.spacedBy(6.dp),
                    contentPadding = PaddingValues(vertical = 12.dp),
                ) {
                    items(viewModel.messages, key = { it.index }) { message -> MessageBubble(message) }
                }
            }
        }
        InputBar(
            value = input,
            onValueChange = { input = it },
            sending = viewModel.isSending,
            error = viewModel.sendError,
            pendingAttachment = pendingAttachment,
            onAttach = { attachmentPicker.launch("*/*") },
            onRemoveAttachment = { pendingAttachment = null },
            onSend = {
                val attachment = pendingAttachment
                viewModel.sendMessage(input, attachment) {
                    input = ""
                    pendingAttachment = null
                }
            },
        )
        StatusBar(tabStatus = viewModel.tabStatus, channelStatus = viewModel.channelStatus)
    }
}

/**
 * Mirrors the desktop app's own bottom status bar -- statusBarText + two
 * lamps (lampBackend, lampChannel) in chat.js/chat.html -- per explicit
 * instruction (2026-09-15): the phone should show exactly what the
 * desktop shows, not just chat text. Same layout order (text left, lamps
 * right) and the same semantic colors/blink rule as chat.js's own
 * updateBackendLamp()/updateChannelLamp() -- see LampColor's own doc
 * comment for the one place chat.js's rule doesn't map 1:1 (there's no
 * "is the WebSocket itself connected" concept on the phone, only the
 * backend-reported state).
 */
@Composable
private fun StatusBar(tabStatus: TabStatus, channelStatus: ChannelStatus) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .padding(horizontal = 12.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            statusBarText(tabStatus),
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f),
        )
        Lamp(tabLampColor(tabStatus))
        Lamp(channelLampColor(channelStatus), modifier = Modifier.padding(start = 6.dp))
    }
}

// state -> lamp color + blink, matching chat.js's updateBackendLamp()
// exactly minus the wsConnected check (no separate transport-level
// connection concept on the phone -- the synced state itself IS the
// source of truth here).
private fun tabLampColor(status: TabStatus): LampColor = when (status.state) {
    "error" -> LampColor.RED
    "recovering" -> LampColor.YELLOW
    "working" -> LampColor.GREEN_BLINK
    else -> LampColor.GREEN
}

private fun statusBarText(status: TabStatus): String = when (status.state) {
    "error" -> status.reason.ifBlank { "Something needs your attention." }
    "recovering" -> status.reason.ifBlank { "Recovering..." }
    "working" -> "Working..."
    else -> "connected"
}

// Matches chat.js's updateChannelLamp() exactly.
private fun channelLampColor(status: ChannelStatus): LampColor = when {
    !status.enabled -> LampColor.YELLOW
    status.last_tick_outcome?.startsWith("threw:") == true -> LampColor.RED
    status.last_tick_outcome?.contains("injecting into headless session") == true -> LampColor.GREEN_BLINK
    else -> LampColor.GREEN
}

private enum class LampColor(val color: Color, val blinking: Boolean) {
    RED(Color(0xFFE53935), false),
    YELLOW(Color(0xFFFFC107), false),
    GREEN(Color(0xFF43A047), false),
    GREEN_BLINK(Color(0xFF43A047), true),
}

/** One status dot -- a plain circle, pulsing alpha when [color]'s
 * blinking flag is set (same idea as chat.js's CSS lamp-blink keyframe:
 * opacity cycles 1 -> 0.25 -> 1). */
@Composable
private fun Lamp(color: LampColor, modifier: Modifier = Modifier) {
    val alpha = if (color.blinking) {
        val transition = rememberInfiniteTransition(label = "lampBlink")
        val animatedAlpha by transition.animateFloat(
            initialValue = 1f,
            targetValue = 0.25f,
            animationSpec = infiniteRepeatable<Float>(
                animation = tween(durationMillis = 900),
                repeatMode = RepeatMode.Reverse,
            ),
            label = "lampBlinkAlpha",
        )
        animatedAlpha
    } else {
        1f
    }
    Box(
        modifier = modifier
            .size(10.dp)
            .alpha(alpha)
            .background(color.color, CircleShape),
    )
}

@Composable
private fun InputBar(
    value: String,
    onValueChange: (String) -> Unit,
    sending: Boolean,
    error: String?,
    pendingAttachment: PendingAttachment?,
    onAttach: () -> Unit,
    onRemoveAttachment: () -> Unit,
    onSend: () -> Unit,
) {
    Column {
        error?.let {
            Text(
                it,
                color = MaterialTheme.colorScheme.error,
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 2.dp),
            )
        }
        pendingAttachment?.let { attachment ->
            Row(
                modifier = Modifier
                    .padding(horizontal = 12.dp, vertical = 2.dp)
                    .background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(8.dp))
                    .padding(horizontal = 8.dp, vertical = 4.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Icon(Icons.Filled.AttachFile, contentDescription = null, modifier = Modifier.size(16.dp))
                Text(
                    attachment.name,
                    style = MaterialTheme.typography.labelMedium,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.padding(start = 4.dp).weight(1f, fill = false),
                )
                IconButton(onClick = onRemoveAttachment, modifier = Modifier.size(24.dp)) {
                    Icon(Icons.Filled.Close, contentDescription = "Remove attachment", modifier = Modifier.size(16.dp))
                }
            }
        }
        Row(
            modifier = Modifier.fillMaxWidth().padding(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            IconButton(onClick = onAttach, enabled = !sending) {
                Icon(Icons.Filled.AttachFile, contentDescription = "Attach a file")
            }
            OutlinedTextField(
                value = value,
                onValueChange = onValueChange,
                placeholder = { Text("Message this tab...") },
                modifier = Modifier.weight(1f),
                maxLines = 4,
            )
            IconButton(onClick = onSend, enabled = (value.isNotBlank() || pendingAttachment != null) && !sending) {
                if (sending) {
                    CircularProgressIndicator(modifier = Modifier.padding(4.dp), strokeWidth = 2.dp)
                } else {
                    Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "Send")
                }
            }
        }
    }
}

@Composable
private fun MessageBubble(message: ChatMessage) {
    val isOwn = message.role == "user"
    val bubbleColor = if (isOwn) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surfaceVariant
    val textColor = if (isOwn) MaterialTheme.colorScheme.onPrimary else MaterialTheme.colorScheme.onSurfaceVariant
    val shape = RoundedCornerShape(
        topStart = 16.dp, topEnd = 16.dp,
        bottomStart = if (isOwn) 16.dp else 4.dp,
        bottomEnd = if (isOwn) 4.dp else 16.dp,
    )
    val clipboard = LocalClipboardManager.current
    var menuOpen by remember { mutableStateOf(false) }

    Column(
        modifier = Modifier.fillMaxWidth(),
        horizontalAlignment = if (isOwn) Alignment.End else Alignment.Start,
    ) {
        Box {
            Box(
                modifier = Modifier
                    .widthIn(max = 280.dp)
                    .background(bubbleColor, shape)
                    .padding(horizontal = 12.dp, vertical = 8.dp)
                    .pointerInput(message.index) {
                        detectTapGestures(onLongPress = { menuOpen = true })
                    },
            ) {
                Column {
                    if (message.text.isNotBlank()) MarkdownText(message.text, color = textColor)
                    message.attachments.forEach { attachment -> AttachmentChip(attachment, textColor) }
                }
            }
            DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                DropdownMenuItem(
                    text = { Text("Copy") },
                    onClick = {
                        menuOpen = false
                        clipboard.setText(AnnotatedString(message.text))
                    },
                )
            }
        }
        if (message.ts > 0) {
            Text(
                formatBubbleTimestamp(message.ts),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.6f),
                modifier = Modifier.padding(top = 2.dp, start = 4.dp, end = 4.dp),
            )
        }
    }
}

/**
 * Real content, per explicit instruction (2026-09-15): companion_api.py's
 * history sync now embeds the actual file bytes (base64), so this renders
 * a genuine inline image for a photo, or a tappable chip that saves the
 * real bytes into this app's cache and opens them with whatever app the
 * phone has for that file type (FileProvider + ACTION_VIEW, same pattern
 * Logger.kt already uses for sharing its own log file). Only falls back to
 * a plain name-only chip when the backend genuinely couldn't embed the
 * bytes (file gone, or over its size cap -- attachment.tooLarge).
 */
@Composable
private fun AttachmentChip(attachment: Attachment, textColor: Color) {
    val context = LocalContext.current
    when {
        attachment.isImage -> {
            val bitmap = remember(attachment.dataBase64) {
                runCatching {
                    val bytes = Base64.decode(attachment.dataBase64, Base64.DEFAULT)
                    BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.asImageBitmap()
                }.getOrNull()
            }
            if (bitmap != null) {
                Image(
                    bitmap = bitmap,
                    contentDescription = attachment.name,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier
                        .padding(top = 4.dp)
                        .heightIn(max = 220.dp)
                        .clip(RoundedCornerShape(10.dp))
                        .clickable { openAttachment(context, attachment) },
                )
            } else {
                AttachmentNameChip(attachment, textColor, context)
            }
        }
        attachment.dataBase64 != null -> AttachmentNameChip(attachment, textColor, context, clickable = true)
        else -> AttachmentNameChip(attachment, textColor, context, clickable = false)
    }
}

@Composable
private fun AttachmentNameChip(
    attachment: Attachment,
    textColor: Color,
    context: android.content.Context,
    clickable: Boolean = false,
) {
    Row(
        modifier = Modifier
            .padding(top = 4.dp)
            .let { if (clickable) it.clickable { openAttachment(context, attachment) } else it },
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(
            Icons.Filled.InsertDriveFile,
            contentDescription = "Attachment",
            tint = textColor,
            modifier = Modifier.size(14.dp),
        )
        Text(
            if (attachment.tooLarge) "${attachment.name} (too large to send)" else attachment.name,
            style = MaterialTheme.typography.labelSmall,
            color = textColor,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(start = 4.dp),
        )
    }
}

/**
 * Decodes the attachment's real bytes into this app's cache dir and opens
 * them with whatever the phone has registered for that mime type --
 * genuinely usable content, not just a name (see AttachmentChip's own doc
 * comment for why this exists).
 */
private fun openAttachment(context: android.content.Context, attachment: Attachment) {
    val dataBase64 = attachment.dataBase64 ?: return
    try {
        val bytes = Base64.decode(dataBase64, Base64.DEFAULT)
        val dir = File(context.cacheDir, "attachments").apply { mkdirs() }
        val file = File(dir, attachment.name)
        file.writeBytes(bytes)
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, attachment.mimeType ?: "application/octet-stream")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(intent)
    } catch (exc: Exception) {
        Logger.e("failed to open attachment ${attachment.name}", exc)
    }
}

// Same idea as the desktop's own chat.js formatTimestamp() (2-digit
// hour:minute, locale AM/PM) -- read there, not touched; ported by hand
// since there's no shared code between the Kotlin and JS sides.
private val BUBBLE_TIME_FORMATTER = java.time.format.DateTimeFormatter.ofPattern("h:mm a")

private fun formatBubbleTimestamp(epochMs: Long): String =
    java.time.Instant.ofEpochMilli(epochMs)
        .atZone(java.time.ZoneId.systemDefault())
        .format(BUBBLE_TIME_FORMATTER)

/**
 * Minimal briefly-visible scrollbar for a LazyColumn -- Compose has no
 * built-in one at this version. Draws a thumb sized/positioned from the
 * list's own layout info; fades a bit after scrolling stops so it reads
 * as an indicator, not chrome.
 */
@Composable
private fun Modifier.verticalScrollbar(state: LazyListState, width: Dp = 3.dp): Modifier {
    val targetAlpha = if (state.isScrollInProgress) 1f else 0.35f
    val alpha by animateFloatAsState(
        targetValue = targetAlpha,
        animationSpec = tween(durationMillis = if (state.isScrollInProgress) 120 else 600),
        label = "scrollbarAlpha",
    )
    val color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.4f)
    return this.drawWithContent {
        drawContent()
        val info = state.layoutInfo
        val totalItems = info.totalItemsCount
        val visibleItems = info.visibleItemsInfo.size
        if (totalItems == 0 || visibleItems == 0 || totalItems <= visibleItems) return@drawWithContent
        val firstVisible = info.visibleItemsInfo.first().index
        val proportionVisible = visibleItems.toFloat() / totalItems
        val scrollbarHeight = size.height * proportionVisible
        val proportionScrolled = firstVisible.toFloat() / (totalItems - visibleItems)
        val scrollbarY = (size.height - scrollbarHeight) * proportionScrolled
        drawRect(
            color = color.copy(alpha = color.alpha * alpha),
            topLeft = Offset(size.width - width.toPx(), scrollbarY),
            size = Size(width.toPx(), scrollbarHeight),
        )
    }
}

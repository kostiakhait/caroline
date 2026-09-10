package com.partnerssolutions.caroline.companion.ui.chat

import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
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
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.partnerssolutions.caroline.companion.data.model.ChatMessage

/**
 * iMessage-style bubbles matching the confirmed Ratatosk-derived
 * direction. Read history over the network + write new messages into
 * tabs/<tabId>/inbox. No local Room cache yet (so no true offline
 * support), no attachments, no reply/edit/delete (those depend on
 * backend capabilities that don't exist yet) -- long-press gives Copy.
 */
@Composable
fun ChatScreen(tabId: String) {
    val factory = viewModelFactory { initializer { ChatViewModel(tabId) } }
    val viewModel: ChatViewModel = viewModel(key = "chat-$tabId", factory = factory)
    val listState = rememberLazyListState()
    var input by remember { mutableStateOf("") }

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
            onSend = { viewModel.sendMessage(input) { input = "" } },
        )
    }
}

@Composable
private fun InputBar(
    value: String,
    onValueChange: (String) -> Unit,
    sending: Boolean,
    error: String?,
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
        Row(
            modifier = Modifier.fillMaxWidth().padding(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            OutlinedTextField(
                value = value,
                onValueChange = onValueChange,
                placeholder = { Text("Message this tab...") },
                modifier = Modifier.weight(1f),
                maxLines = 4,
            )
            IconButton(onClick = onSend, enabled = value.isNotBlank() && !sending) {
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
                Text(message.text, color = textColor)
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
    }
}

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

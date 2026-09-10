package com.partnerssolutions.caroline.companion.ui.chat

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.partnerssolutions.caroline.companion.data.model.ChatMessage

/**
 * Read-only for now (see ChatViewModel's own doc comment) -- iMessage-style
 * bubbles matching the confirmed Ratatosk-derived direction (rounded, tail
 * corner toward the sender's own side), no attachments/Room cache/compose
 * yet. Sending INTO tabs/<tabId>/inbox is the natural next piece.
 */
@Composable
fun ChatScreen(tabId: String) {
    val factory = viewModelFactory { initializer { ChatViewModel(tabId) } }
    val viewModel: ChatViewModel = viewModel(key = "chat-$tabId", factory = factory)

    Box(modifier = Modifier.fillMaxSize()) {
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
                modifier = Modifier.fillMaxSize().padding(horizontal = 12.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp),
                contentPadding = androidx.compose.foundation.layout.PaddingValues(vertical = 12.dp),
            ) {
                items(viewModel.messages, key = { it.index }) { message -> MessageBubble(message) }
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
    Column(
        modifier = Modifier.fillMaxWidth(),
        horizontalAlignment = if (isOwn) Alignment.End else Alignment.Start,
    ) {
        Box(
            modifier = Modifier
                .widthIn(max = 280.dp)
                .background(bubbleColor, shape)
                .padding(horizontal = 12.dp, vertical = 8.dp),
        ) {
            Text(message.text, color = textColor)
        }
    }
}

package com.partnerssolutions.caroline.companion.ui.chat

import android.widget.TextView
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.viewinterop.AndroidView
import io.noties.markwon.Markwon

/**
 * Renders markdown as real formatted text (headers, bold/italic, lists,
 * inline/fenced code) -- matching the desktop app's own chat UI, which
 * renders markdown rather than showing raw `**`/`#`/backtick syntax.
 * Markwon (a real, maintained Android markdown renderer) over a plain
 * TextView via AndroidView -- Compose has no built-in rich-text markdown
 * renderer, and hand-rolling one would just be a worse Markwon.
 */
@Composable
fun MarkdownText(markdown: String, color: Color, modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val markwon = remember(context) { Markwon.create(context) }
    val colorArgb = color.toArgb()
    AndroidView(
        modifier = modifier,
        factory = { ctx ->
            TextView(ctx).apply {
                setTextColor(colorArgb)
                textSize = 16f
            }
        },
        update = { textView ->
            textView.setTextColor(colorArgb)
            markwon.setMarkdown(textView, markdown)
        },
    )
}

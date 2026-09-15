package com.partnerssolutions.caroline.companion.data.model

/**
 * One entry from a ChatMessage's "attachments" field. Per explicit
 * instruction (2026-09-15, "точное соответствие... в обе стороны"),
 * companion_api.py's history sync now embeds the REAL file bytes read off
 * the desktop's own workspace/uploads/ directory (base64) alongside the
 * name, not just a filename -- there is still no separate file-serving
 * channel between the phone and Caroline's backend, only the var:* KV
 * store, so the bytes travel inline in the same JSON blob. [dataBase64] is
 * null only when the backend couldn't embed it (file missing, or over
 * companion_api.py's MAX_EMBEDDED_ATTACHMENT_BYTES cap, flagged by
 * [tooLarge]) -- that's the one remaining case rendered as a plain name
 * chip instead of real content.
 */
data class Attachment(
    val name: String,
    val mimeType: String? = null,
    val dataBase64: String? = null,
    val tooLarge: Boolean = false,
) {
    val isImage: Boolean get() = mimeType?.startsWith("image/") == true && dataBase64 != null
}

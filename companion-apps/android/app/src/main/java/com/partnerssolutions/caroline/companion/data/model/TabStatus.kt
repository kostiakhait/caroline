package com.partnerssolutions.caroline.companion.data.model

/**
 * Mirrors `tabs/<tabId>/status` (companion_api.py's _sync_status, sourced
 * from chat_session.py's own _compute_public_status() -- the SAME value
 * that drives the desktop app's lamp 1 + status-bar text). state is one
 * of "ready" | "working" | "recovering" | "error"; reason is a short
 * human-readable explanation, non-empty only for "recovering"/"error".
 */
data class TabStatus(
    val state: String,
    val reason: String,
) {
    companion object {
        val READY = TabStatus("ready", "")
    }
}

/**
 * Mirrors the global `channel_status` key (companion_api.py's
 * _sync_status, sourced from ratatosk_channel.get_ratatosk_channel_status()
 * -- the SAME value that drives the desktop app's lamp 2). Field names
 * match the Python ChannelStatus dataclass exactly (asdict(), no camelCase
 * conversion happens anywhere in this pipeline).
 */
data class ChannelStatus(
    val enabled: Boolean = false,
    @Suppress("unused") val tick_count: Int = 0,
    @Suppress("unused") val last_tick_at_iso: String? = null,
    val last_tick_outcome: String? = null,
    @Suppress("unused") val caroline_email: String? = null,
) {
    companion object {
        val UNKNOWN = ChannelStatus()
    }
}

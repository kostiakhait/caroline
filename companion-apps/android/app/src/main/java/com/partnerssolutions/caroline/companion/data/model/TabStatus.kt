package com.partnerssolutions.caroline.companion.data.model

/**
 * Mirrors `tabs/<tabId>/status` (companion_api.py's _sync_status, sourced
 * from chat_session.py's own _compute_public_status() -- the SAME value
 * that drives the desktop app's status lamp + status-bar text). state is
 * one of "ready" | "working" | "recovering" | "error"; reason is a short
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

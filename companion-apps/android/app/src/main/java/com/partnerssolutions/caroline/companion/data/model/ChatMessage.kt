package com.partnerssolutions.caroline.companion.data.model

/**
 * One entry as synced from `tabs/<tabId>/history/<index>` (see
 * companion_api.py's _sync_history -- one Camerlengo key per message, not
 * one overwritten blob, so the phone only ever pulls entries it hasn't
 * seen). `role` mirrors history.py's HistoryEntry: "user" | "assistant".
 *
 * Skeleton note: this is the wire shape, not yet a Room @Entity -- the
 * local offline-first database (mirroring Ratatosk's MessageEntity/
 * MessageDao) is unbuilt. Attachments are entirely unmodeled here yet.
 */
data class ChatMessage(
    val index: Int,
    val role: String,
    val text: String,
    val ts: Long,
)

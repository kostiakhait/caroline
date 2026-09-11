package com.partnerssolutions.caroline.companion.util

/**
 * Client-side cleanup of raw synced message text -- explicit instruction
 * (2026-09-11): the backend is a GIVEN for this app, never something to
 * touch for a presentation concern like this. Every message synced from
 * tabs/<tabId>/history/<index> carries the SAME raw content the desktop's
 * own model-facing session sees, including:
 *  - a leading "[Sent: ...]"/weekday timestamp stamp (meant for the
 *    model's own time-awareness, not a human reader -- the desktop's own
 *    chat.js never shows this text either, it renders the real ts as a
 *    small caption under the bubble instead, see ChatMessage.ts and
 *    MessageBubble's own timestamp caption).
 *  - occasionally a whole synthetic/internal turn (an inject_proactive()
 *    nudge -- watchdog checks, startup greetings, etc.), tagged with a
 *    fixed marker (mirrors the backend's own _SYNTHETIC_TURN_MARKER,
 *    chat_session.py) wrapped in U+2063 invisible separators so it never
 *    renders as visible clutter if it ever slips through unfiltered
 *    somewhere else.
 * Ported here (not imported -- there's no shared package between the
 * Kotlin and Python sides) from backend-py/app/chat_session.py's own
 * _is_synthetic_history_text/_SYNTHETIC_HISTORY_TEXT_PATTERNS, kept in
 * sync by hand.
 */
object TextFiltering {
    private val STAMP_PATTERN = Regex(
        """^\[(Sent: |(Sun|Mon|Tue|Wed|Thu|Fri|Sat), )[^]]*]\s*""",
        RegexOption.IGNORE_CASE,
    )

    private const val SYNTHETIC_TURN_MARKER = "⁣[[caroline-internal-turn]]⁣"

    private val SYNTHETIC_LINE_PATTERNS = listOf(
        Regex("""^API Error:""", RegexOption.IGNORE_CASE),
        Regex("""^\[System note:""", RegexOption.IGNORE_CASE),
        Regex("""^\[Caroline was restarted""", RegexOption.IGNORE_CASE),
        Regex("""^Continue from where you left off\.?$""", RegexOption.IGNORE_CASE),
        Regex("""^Continue any unfinished work, if there is any\.""", RegexOption.IGNORE_CASE),
        Regex("""^You just started up \(or restarted\)\.""", RegexOption.IGNORE_CASE),
        Regex("""^\[Internal:""", RegexOption.IGNORE_CASE),
        Regex("""^\[The user just stopped what you were doing""", RegexOption.IGNORE_CASE),
        Regex("""^No response requested\.?$""", RegexOption.IGNORE_CASE),
        Regex("""^<"""),
        Regex("""^⏰ Reminder due"""), // "⏰ Reminder due"
        Regex("""^The app is closing right now\.""", RegexOption.IGNORE_CASE),
    )

    /** Strips a leading "[Sent: ...]" stamp for DISPLAY only. */
    fun stripStamp(raw: String): String = STAMP_PATTERN.replace(raw, "").trim()

    /** True if, once the stamp is stripped, this is empty or a known
     * internal/synthetic turn rather than something a human wrote. */
    fun isSyntheticText(raw: String): Boolean {
        val stripped = stripStamp(raw)
        if (stripped.isBlank()) return true
        if (stripped.contains(SYNTHETIC_TURN_MARKER) || stripped.contains("[[caroline-internal-turn]]")) return true
        if (stripped.contains("[[NO_UPDATE]]")) return true
        return SYNTHETIC_LINE_PATTERNS.any { it.containsMatchIn(stripped) }
    }
}

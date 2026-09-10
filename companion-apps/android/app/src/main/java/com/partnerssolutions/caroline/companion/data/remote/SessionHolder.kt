package com.partnerssolutions.caroline.companion.data.remote

/**
 * Skeleton placeholder -- in-memory only, lost on process death. The real
 * version needs to persist the user's EMAIL+PASSWORD (not the session
 * token itself, which expires) via DataStore/EncryptedSharedPreferences,
 * mirroring the desktop backend's own ~/.mcp-notes/credentials.json +
 * "mint a fresh v2 session per call" pattern (see login_api.py). Not built
 * yet -- every screen that needs a session currently has to go through
 * CamerlengoRepository.login() again after a process restart.
 */
object SessionHolder {
    var session: String? = null
        private set

    fun set(value: String) {
        session = value
    }

    fun clear() {
        session = null
    }
}

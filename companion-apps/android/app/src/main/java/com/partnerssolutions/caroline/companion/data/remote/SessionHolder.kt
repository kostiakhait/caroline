package com.partnerssolutions.caroline.companion.data.remote

/**
 * The live v2 SESSION TOKEN only -- deliberately in-memory-only, lost on
 * process death, same as the desktop backend's own choice (login_api.py's
 * get_v2_session(): a v2 session dies after a server-side idle timeout
 * anyway, so there's little point persisting the token itself). The
 * user's actual EMAIL+PASSWORD are what's persisted (CredentialsStore,
 * encrypted) -- CamerlengoRepository re-mints a session from those
 * automatically (ensureSession/withSession) whenever this is empty or a
 * call reports it expired, so a process restart does NOT require the user
 * to type their password again.
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

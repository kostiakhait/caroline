package com.partnerssolutions.caroline.companion.data.remote

import com.partnerssolutions.caroline.companion.util.Logger

/**
 * Mirrors backend-py/app/login_api.py's get_v2_session() + app/plugins/
 * companion_api.py's get_mine/set_mine/get_all_mine/delete_mine -- same
 * "mint a fresh v2 session per call, never persist the token" choice the
 * desktop backend already made (a v2 session dies after a server-side
 * idle timeout; simpler to just re-mint than to track expiry here too).
 *
 * The email+password themselves ARE persisted (CredentialsStore, encrypted
 * at rest) -- every method here manages its own session transparently:
 * mints one from stored credentials if none is held yet (ensureSession),
 * and re-mints + retries ONCE if a call comes back with a session-expired
 * error (withSession), so callers never see or pass a session token at all.
 */
class CamerlengoRepository(private val api: CamerlengoApi = CamerlengoModule.api) {

    class CamerlengoException(message: String) : Exception(message)
    class NotLoggedInException : Exception("Not logged in.")

    suspend fun login(email: String, password: String): String {
        val response = api.call(
            VarCommandRequest(
                command = "user:verify",
                key = CAMERLENGO_LOGIN_SERVICE_KEY,
                path = "/users",
                user = email,
                password = password,
            ),
        )
        val session = response.session ?: throw CamerlengoException(response.reason ?: "Login failed")
        SessionHolder.set(session)
        CredentialsStore.save(email, password)
        return session
    }

    fun logout() {
        SessionHolder.clear()
        CredentialsStore.clear()
    }

    /** True if we can plausibly act without prompting for a password --
     * either a live session, or stored credentials to mint one from. */
    fun canAutoLogin(): Boolean = SessionHolder.session != null || CredentialsStore.load() != null

    private suspend fun ensureSession(): String {
        SessionHolder.session?.let { return it }
        val creds = CredentialsStore.load() ?: throw NotLoggedInException()
        Logger.i("re-minting session from stored credentials")
        return login(creds.email, creds.password)
    }

    /** Runs `block` with a live session, re-minting from stored
     * credentials and retrying ONCE if the call reports an expired/
     * invalid session -- same one-retry policy as the Python client. */
    private suspend fun <T> withSession(block: suspend (String) -> T): T {
        val session = ensureSession()
        return try {
            block(session)
        } catch (exc: CamerlengoException) {
            if (exc.message?.contains("session", ignoreCase = true) != true) throw exc
            val creds = CredentialsStore.load() ?: throw exc
            Logger.i("session expired mid-call, re-logging in and retrying once")
            val fresh = login(creds.email, creds.password)
            block(fresh)
        }
    }

    suspend fun getMine(path: String): Any? = withSession { session ->
        val response = api.call(VarCommandRequest(command = "var:getMine", session = session, path = path))
        if (!response.ok) {
            if (response.reason?.contains("no such variable", ignoreCase = true) == true) return@withSession null
            throw CamerlengoException(response.reason ?: "var:getMine failed")
        }
        response.value
    }

    suspend fun getAllMine(path: String = ""): Map<*, *> = withSession { session ->
        val response = api.call(VarCommandRequest(command = "var:getAllMine", session = session, path = path))
        if (!response.ok) {
            if (response.reason?.contains("no such variable", ignoreCase = true) == true) return@withSession emptyMap<Any, Any>()
            throw CamerlengoException(response.reason ?: "var:getAllMine failed")
        }
        response.value as? Map<*, *> ?: emptyMap<Any, Any>()
    }

    suspend fun setMine(path: String, value: Any?) = withSession { session ->
        val response = api.call(VarCommandRequest(command = "var:setMine", session = session, path = path, value = value))
        if (!response.ok) throw CamerlengoException(response.reason ?: "var:setMine failed")
    }

    suspend fun deleteMine(path: String) = withSession { session ->
        val response = api.call(VarCommandRequest(command = "var:deleteMine", session = session, path = path))
        if (!response.ok) throw CamerlengoException(response.reason ?: "var:deleteMine failed")
    }
}

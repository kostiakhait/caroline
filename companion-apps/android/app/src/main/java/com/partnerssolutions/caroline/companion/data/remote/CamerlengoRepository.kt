package com.partnerssolutions.caroline.companion.data.remote

import com.partnerssolutions.caroline.companion.util.Logger

// Per explicit instruction (2026-10-02), after a real incident where a
// companion_list_contacts request was received and handled by
// CompanionOpsService but neither the backend nor the phone's own log had
// ANY record of what happened to the response write (setMine logged
// nothing either way) -- every call below now logs its own start, elapsed
// time, and outcome, so a future hang shows up as "started, never
// finished" instead of a silent gap indistinguishable from "never even
// tried". Timing in particular is what would have told the two stories
// (backend never saw a real response vs. this call itself hung) apart.
private suspend inline fun <T> timedCall(label: String, block: suspend () -> T): T {
    val start = System.currentTimeMillis()
    Logger.i("$label: starting")
    try {
        val result = block()
        Logger.i("$label: ok (${System.currentTimeMillis() - start}ms)")
        return result
    } catch (exc: Exception) {
        Logger.w("$label: failed (${System.currentTimeMillis() - start}ms)", exc)
        throw exc
    }
}

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

    suspend fun login(rawEmail: String, password: String): String = timedCall("login") {
        // Camerlengo namespaces everything by the exact login string
        // (caroline/<login>/...), so "Kostia@X.com" and "kostia@x.com" are
        // two different, empty-looking accounts. Always lowercase (and trim)
        // here -- the one place every login, including the automatic
        // re-login from stored credentials, goes through -- so the phone
        // always lands in the same namespace the desktop writes to.
        val email = rawEmail.trim().lowercase()
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
        session
    }

    fun logout() {
        Logger.i("logout")
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

    /** Speech to text through Caroline's `ai:stt` -- same call the desktop makes. Null if nothing was recognized. */
    suspend fun speechToText(base64Audio: String, format: String): String? = timedCall("speechToText") {
        withSession { session ->
            val response = api.call(
                VarCommandRequest(command = "ai:stt", key = CAROLINE_AI_SERVICE_KEY, session = session, audio = base64Audio, format = format),
            )
            if (!response.ok) throw CamerlengoException(response.reason ?: "ai:stt failed")
            response.result?.takeIf { it.isNotBlank() }
        }
    }

    /** Text to speech through `ai:tts`; returns base64 MP3. */
    suspend fun textToSpeech(text: String, voice: String = "Nova"): String = timedCall("textToSpeech") {
        withSession { session ->
            val response = api.call(
                VarCommandRequest(command = "ai:tts", key = CAROLINE_AI_SERVICE_KEY, session = session, text = text, voice = voice),
            )
            if (!response.ok) throw CamerlengoException(response.reason ?: "ai:tts failed")
            response.result ?: throw CamerlengoException("ai:tts returned no audio")
        }
    }

    suspend fun getMine(path: String): Any? = timedCall("getMine($path)") {
        withSession { session ->
            val response = api.call(VarCommandRequest(command = "var:getMine", session = session, path = path))
            if (!response.ok) {
                if (response.reason?.contains("no such variable", ignoreCase = true) == true) {
                    Logger.i("getMine($path): no such variable -- treating as null")
                    return@withSession null
                }
                throw CamerlengoException(response.reason ?: "var:getMine failed")
            }
            response.value
        }
    }

    suspend fun getAllMine(path: String = ""): Map<*, *> = timedCall("getAllMine($path)") {
        withSession { session ->
            val response = api.call(VarCommandRequest(command = "var:getAllMine", session = session, path = path))
            if (!response.ok) {
                if (response.reason?.contains("no such variable", ignoreCase = true) == true) {
                    Logger.i("getAllMine($path): no such variable -- treating as empty")
                    return@withSession emptyMap<Any, Any>()
                }
                throw CamerlengoException(response.reason ?: "var:getAllMine failed")
            }
            (response.value as? Map<*, *> ?: emptyMap<Any, Any>()).also {
                Logger.i("getAllMine($path): ${it.size} entr${if (it.size == 1) "y" else "ies"}")
            }
        }
    }

    suspend fun setMine(path: String, value: Any?) = timedCall("setMine($path)") {
        withSession { session ->
            val response = api.call(VarCommandRequest(command = "var:setMine", session = session, path = path, value = value))
            if (!response.ok) throw CamerlengoException(response.reason ?: "var:setMine failed")
        }
    }

    suspend fun deleteMine(path: String) = timedCall("deleteMine($path)") {
        withSession { session ->
            val response = api.call(VarCommandRequest(command = "var:deleteMine", session = session, path = path))
            if (!response.ok) throw CamerlengoException(response.reason ?: "var:deleteMine failed")
        }
    }
}

package com.partnerssolutions.caroline.companion.data.remote

/**
 * Mirrors backend-py/app/login_api.py's get_v2_session() + app/plugins/
 * companion_api.py's get_mine/set_mine/get_all_mine/delete_mine -- same
 * "mint a fresh v2 session per call, never persist the token" choice the
 * desktop backend already made (a v2 session dies after a server-side
 * idle timeout; simpler to just re-mint than to track expiry here too).
 *
 * Credentials themselves ARE persisted (see LoginRepository, DataStore --
 * not yet built) so the user only logs in once; this class only ever
 * takes them as parameters, never stores them.
 */
class CamerlengoRepository(private val api: CamerlengoApi = CamerlengoModule.api) {

    class CamerlengoException(message: String) : Exception(message)

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
        return response.session ?: throw CamerlengoException(response.reason ?: "Login failed")
    }

    suspend fun getMine(session: String, path: String): Any? {
        val response = api.call(VarCommandRequest(command = "var:getMine", session = session, path = path))
        if (!response.ok) {
            // "no such variable found" is a normal, expected absence, not
            // an error worth surfacing -- same treatment as the Python
            // client's own get_mine().
            if (response.reason?.contains("no such variable", ignoreCase = true) == true) return null
            throw CamerlengoException(response.reason ?: "var:getMine failed")
        }
        return response.value
    }

    suspend fun getAllMine(session: String, path: String = ""): Map<*, *> {
        val response = api.call(VarCommandRequest(command = "var:getAllMine", session = session, path = path))
        if (!response.ok) {
            if (response.reason?.contains("no such variable", ignoreCase = true) == true) return emptyMap<Any, Any>()
            throw CamerlengoException(response.reason ?: "var:getAllMine failed")
        }
        return response.value as? Map<*, *> ?: emptyMap<Any, Any>()
    }

    suspend fun setMine(session: String, path: String, value: Any?) {
        val response = api.call(VarCommandRequest(command = "var:setMine", session = session, path = path, value = value))
        if (!response.ok) throw CamerlengoException(response.reason ?: "var:setMine failed")
    }

    suspend fun deleteMine(session: String, path: String) {
        val response = api.call(VarCommandRequest(command = "var:deleteMine", session = session, path = path))
        if (!response.ok) throw CamerlengoException(response.reason ?: "var:deleteMine failed")
    }
}

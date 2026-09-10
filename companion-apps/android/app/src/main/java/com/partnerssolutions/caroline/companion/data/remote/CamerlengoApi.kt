package com.partnerssolutions.caroline.companion.data.remote

import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass
import retrofit2.http.Body
import retrofit2.http.POST

/**
 * Camerlengo's v2 API is ONE JSON-RPC-ish endpoint (POST /), dispatched by
 * a "command" field in the body -- not per-command URL paths. This mirrors
 * that directly rather than pretending it's a REST resource per command.
 * See the caroline-android-companion plan and reforce's
 * API/Api2VariableCommands.py for the authoritative protocol -- every
 * field/command name here must match that file exactly, this is a client,
 * not an independent spec.
 *
 * Only the four session-scoped ("Mine") variable commands this app
 * actually needs are covered: var:getMine/setMine/getAllMine/deleteMine.
 * All auto-prefix to this account's own caroline/<login>/ subtree
 * server-side -- this client never builds that prefix itself.
 */
interface CamerlengoApi {
    @POST(".")
    suspend fun call(@Body request: VarCommandRequest): VarCommandResponse
}

@JsonClass(generateAdapter = true)
data class VarCommandRequest(
    val command: String,
    val session: String? = null,
    // Login-only fields (command = "user:verify") -- see mint_v2_session in
    // the main repo's backend-py/app/plugins/sw_api.py for the server-side
    // twin of this exact call.
    val key: String? = null,
    val path: String? = null,
    val user: String? = null,
    val password: String? = null,
    val value: Any? = null,
)

@JsonClass(generateAdapter = true)
data class VarCommandResponse(
    @Json(name = ".status") val status: String? = null,
    @Json(name = ".reason") val reason: String? = null,
    @Json(name = ".errcode") val errcode: String? = null,
    @Json(name = ".value") val value: Any? = null,
    val session: String? = null,
) {
    val ok: Boolean get() = status == "ok"
}

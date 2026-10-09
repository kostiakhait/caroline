package com.partnerssolutions.caroline.companion.data.remote

import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import java.util.concurrent.TimeUnit
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.moshi.MoshiConverterFactory

/**
 * Same base URL as the main repo's backend-py/app/plugins/sw_api.py's
 * API_URL constant -- one shared Camerlengo endpoint, not something this
 * app owns or can vary per-user.
 */
private const val CAMERLENGO_BASE_URL = "https://www.squirrelwisdom.com/"

/**
 * V2_LOGIN_SERVICE_KEY from the main repo's sw_api.py -- public/service key
 * scoped only to user:verify (login), not a secret specific to any one
 * user. Duplicated here rather than shared across repos because there is
 * no shared-constants package between the Kotlin and Python sides (same
 * tradeoff PRIMARY_TAB_ID makes on the backend, see companion_api.py's own
 * comment on it).
 */
// Caroline's own general-purpose SquirrelWisdom service key for ai:stt/ai:tts --
// the same one the desktop backend uses (backend-py/app/plugins/sw_api.py's
// CAROLINE_SW_KEY); a service key, not a user secret.
const val CAROLINE_AI_SERVICE_KEY = "QvR-sujLOgpKWZ-yhSOK5ZNgEe4sgF0EUU7GexQqr4M"
const val CAMERLENGO_LOGIN_SERVICE_KEY = "fytZDwOTaBo8I173IS2DaY_qgzm0IFvqvnxJGvC5QrE"

object CamerlengoModule {
    val api: CamerlengoApi by lazy {
        val moshi = Moshi.Builder().add(KotlinJsonAdapterFactory()).build()
        val logging = HttpLoggingInterceptor().apply {
            // Body-level logging would put SW passwords/session tokens in
            // logcat -- headers only, and there are none of interest here
            // either (auth travels in the JSON body, per Camerlengo's own
            // protocol), so this stays at BASIC (method/URL/status only).
            level = HttpLoggingInterceptor.Level.BASIC
        }
        // Explicit timeouts, including an OVERALL callTimeout -- without one a
        // single stalled Camerlengo call (common when SquirrelWisdom/navlink is
        // flaky) hangs forever, and because every CamerlengoRepository call runs
        // inside CompanionOpsService.pollLoop's one coroutine, that one hung
        // call freezes the WHOLE poll loop: heartbeat stops, and no SMS/contacts/
        // sms_query request is ever processed again until the 15-minute watchdog
        // happens to restart the service. Confirmed live 2026-10-09: the loop sat
        // frozen (heartbeat stale, requests unprocessed) for 15+ minutes. A bounded
        // callTimeout makes the call throw instead, the poll tick's try/catch
        // swallows it, and the loop keeps ticking -- self-healing against a flaky
        // network. 45s is generous for the large SMS-sync payload yet still bounded.
        val client = OkHttpClient.Builder()
            .addInterceptor(logging)
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .writeTimeout(45, TimeUnit.SECONDS)
            .callTimeout(60, TimeUnit.SECONDS)
            .build()
        Retrofit.Builder()
            .baseUrl(CAMERLENGO_BASE_URL)
            .client(client)
            .addConverterFactory(MoshiConverterFactory.create(moshi))
            .build()
            .create(CamerlengoApi::class.java)
    }
}

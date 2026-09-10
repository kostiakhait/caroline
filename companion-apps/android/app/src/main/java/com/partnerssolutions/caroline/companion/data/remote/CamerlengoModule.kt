package com.partnerssolutions.caroline.companion.data.remote

import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
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
        val client = OkHttpClient.Builder().addInterceptor(logging).build()
        Retrofit.Builder()
            .baseUrl(CAMERLENGO_BASE_URL)
            .client(client)
            .addConverterFactory(MoshiConverterFactory.create(moshi))
            .build()
            .create(CamerlengoApi::class.java)
    }
}

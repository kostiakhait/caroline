package com.partnerssolutions.caroline.companion.data.remote

import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.moshi.MoshiConverterFactory
import java.util.concurrent.TimeUnit

private const val NAVLINK_BASE_URL = "https://navlink.net/"

object NavlinkModule {
    // Shared with UpdateChecker for the actual APK download (raw OkHttp, not
    // Retrofit -- that one streams a multi-MB body to a file with a running
    // SHA-256 digest, not a JSON response). A longer read timeout than the
    // JSON client's default, since a multi-MB download legitimately takes
    // longer than a single metadata request.
    val downloadClient: OkHttpClient by lazy {
        OkHttpClient.Builder().readTimeout(60, TimeUnit.SECONDS).build()
    }

    val api: NavlinkApi by lazy {
        val moshi = Moshi.Builder().add(KotlinJsonAdapterFactory()).build()
        val logging = HttpLoggingInterceptor().apply { level = HttpLoggingInterceptor.Level.BASIC }
        val client = OkHttpClient.Builder().addInterceptor(logging).build()
        Retrofit.Builder()
            .baseUrl(NAVLINK_BASE_URL)
            .client(client)
            .addConverterFactory(MoshiConverterFactory.create(moshi))
            .build()
            .create(NavlinkApi::class.java)
    }
}

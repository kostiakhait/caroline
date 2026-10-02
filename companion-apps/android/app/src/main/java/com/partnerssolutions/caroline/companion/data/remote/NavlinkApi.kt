package com.partnerssolutions.caroline.companion.data.remote

import com.squareup.moshi.JsonClass
import retrofit2.http.GET
import retrofit2.http.Query

/**
 * Public, unauthenticated apps.navlink.net storefront API (snc-arbiter's
 * apps_api.go) -- the same source companion-apps/android/deploy.bat's
 * storefront sync writes to (see its UPLOAD_KEY-authenticated POST
 * /admin/downloads/upload). GET /api/apps/list needs no auth and is what
 * UpdateChecker polls: version/sha256 for this app's android download are
 * kept in sync automatically on every real deploy (admin_downloads.go's
 * clientBinaryToListing), so there is nothing else to maintain here.
 */
interface NavlinkApi {
    @GET("api/apps/list")
    suspend fun listApps(@Query("q") query: String, @Query("os") os: String): List<AppListing>
}

@JsonClass(generateAdapter = true)
data class AppListing(
    val id: Long,
    val name: String,
    val downloads: List<AppDownload> = emptyList(),
)

@JsonClass(generateAdapter = true)
data class AppDownload(
    val platform: String,
    val url: String,
    val version: String? = null,
    val sha256: String? = null,
)

package com.partnerssolutions.caroline.companion.update

import android.content.Context
import android.content.Intent
import com.partnerssolutions.caroline.companion.BuildConfig
import com.partnerssolutions.caroline.companion.data.remote.NavlinkModule
import com.partnerssolutions.caroline.companion.util.Logger
import kotlinx.coroutines.runBlocking
import okhttp3.Request
import java.io.File
import java.io.IOException
import java.security.MessageDigest

private const val PREFS_NAME = "caroline_companion_update"
private const val PREF_READY_VERSION = "ready_version"
private const val PREF_READY_PATH = "ready_path"
private const val CHECK_INTERVAL_MS = 5 * 60 * 1000L
private const val APK_FILE_NAME = "caroline-update.apk"
const val ACTION_UPDATE_READY = "com.partnerssolutions.caroline.companion.UPDATE_READY"

/** Outcome of UpdateChecker.checkNow() -- see its own doc comment. */
sealed class CheckResult {
    data class UpToDate(val currentVersion: String) : CheckResult()
    data class ReadyToInstall(val version: String) : CheckResult()
    data class Error(val message: String) : CheckResult()
}

/**
 * Same OTA pattern as ShortNerdCat's own UpdateChecker (snc/android/app/src/
 * main/kotlin/com/shortnerdcat/snc/UpdateChecker.kt): poll the public
 * apps.navlink.net storefront API, and when a newer version is published,
 * download + SHA-256-verify the APK silently in the background on this
 * checker's own daemon thread -- the user sees nothing until the file is
 * sitting on disk, verified and ready to install. MainActivity/
 * CompanionTabsScreen only need to surface an "Update ready" menu entry and
 * launch the installer from the persisted path -- no waiting, no re-download.
 *
 * Deliberately simpler than ShortNerdCat's: that app routes OTA traffic
 * around its own VPN tunnel (VpnService.protect()/a local SOCKS5 fallback)
 * and tries multiple navlink_mirror forwarders to survive DPI blocking --
 * none of that applies here, this app has no tunnel and no censorship
 * concern, so a plain HTTPS request to navlink.net is enough. The polled
 * endpoint differs too: ShortNerdCat's VPN clients go through snc-arbiter's
 * dedicated OTA relay (/client-<slug>...), reserved for otaSlugs; Caroline
 * isn't in that list (see admin_downloads.go's own comment), so this reads
 * the general-purpose public storefront listing (GET /api/apps/list) that
 * every apps.navlink.net card already exposes -- same version/sha256 the
 * card itself displays, kept in sync automatically by every real deploy.
 */
class UpdateChecker(private val context: Context) {

    @Volatile private var running = false
    private var thread: Thread? = null

    fun start() {
        if (running) return
        running = true
        thread = Thread({ loop() }, "caroline-update-checker").apply { isDaemon = true; start() }
    }

    fun stop() {
        running = false
        thread?.interrupt()
    }

    private fun loop() {
        while (running) {
            try {
                checkNow()
            } catch (_: InterruptedException) {
                break
            } catch (e: Exception) {
                Logger.w("UpdateChecker: check error", e)
            }
            try {
                Thread.sleep(CHECK_INTERVAL_MS)
            } catch (_: InterruptedException) {
                break
            }
        }
    }

    /**
     * Per explicit instruction (2026-10-05): a manual "Check for updates..."
     * menu entry (same idea as WildCat's), because the periodic loop's own
     * "already up to date" path (below) logs nothing at all -- with every
     * prior release installed manually, right after a deploy, the loop's
     * silent branch is the ONLY one that ever actually ran in practice, so
     * there was never any log evidence either way of whether the silent
     * background path genuinely works. This is the SAME logic the loop
     * calls every 5 minutes, just also returning what happened so a manual
     * tap can show it, instead of only ever logging it. Blocking network
     * I/O -- call from a background thread/dispatcher, same as the loop
     * already does on its own dedicated thread.
     */
    fun checkNow(): CheckResult {
        val listings = try {
            runBlocking { NavlinkModule.api.listApps("Caroline", "android") }
        } catch (e: Exception) {
            Logger.w("UpdateChecker: fetch failed", e)
            return CheckResult.Error("Couldn't reach the update server: ${e.message}")
        }
        val download = listings.firstOrNull()?.downloads?.firstOrNull { it.platform == "android" }
        if (download == null) {
            Logger.w("UpdateChecker: no android download entry in storefront listing")
            return CheckResult.Error("No Android build listed on the server.")
        }
        val version = download.version?.trim().orEmpty()
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        if (!isValidVersion(version)) {
            Logger.w("UpdateChecker: invalid version string '$version'")
            return CheckResult.Error("The server reported an invalid version string ('$version').")
        }
        if (version <= BuildConfig.VERSION_NAME) {
            clearAll(prefs)
            return CheckResult.UpToDate(BuildConfig.VERSION_NAME)
        }

        // Already downloaded and verified for this exact version -- nothing
        // to do, the "ready" broadcast was already sent when it finished.
        if (prefs.getString(PREF_READY_VERSION, null) == version &&
            File(prefs.getString(PREF_READY_PATH, "") ?: "").exists()
        ) {
            return CheckResult.ReadyToInstall(version)
        }

        Logger.i("UpdateChecker: update available $version (current ${BuildConfig.VERSION_NAME}) -- downloading")
        val apk = downloadAndVerify(download.url, download.sha256.orEmpty())
        if (apk == null) {
            Logger.w("UpdateChecker: download/verification failed for $version -- will retry on next check")
            return CheckResult.Error("Found version $version but the download failed -- will retry automatically.")
        }
        Logger.i("UpdateChecker: update $version downloaded and verified -- ready to install")
        prefs.edit()
            .putString(PREF_READY_VERSION, version)
            .putString(PREF_READY_PATH, apk.absolutePath)
            .apply()
        context.sendBroadcast(Intent(ACTION_UPDATE_READY).setPackage(context.packageName))
        return CheckResult.ReadyToInstall(version)
    }

    // Downloads to a stable location in filesDir (survives cache eviction --
    // the file must still be there whenever the user taps "Install",
    // possibly much later) and verifies its SHA-256 against the storefront-
    // provided digest. Three attempts -- a transient network hiccup
    // shouldn't block the whole update.
    private fun downloadAndVerify(url: String, expectedSha256: String): File? {
        val dir = File(context.filesDir, "updates").apply { mkdirs() }
        val out = File(dir, APK_FILE_NAME)
        repeat(3) { attempt ->
            try {
                val request = Request.Builder().url(url).build()
                NavlinkModule.downloadClient.newCall(request).execute().use { resp ->
                    if (!resp.isSuccessful) throw IOException("HTTP ${resp.code}")
                    val body = resp.body ?: throw IOException("empty response body")
                    val digest = MessageDigest.getInstance("SHA-256")
                    out.outputStream().use { fileOut ->
                        body.byteStream().use { input ->
                            val buf = ByteArray(8192)
                            while (true) {
                                val n = input.read(buf)
                                if (n == -1) break
                                fileOut.write(buf, 0, n)
                                digest.update(buf, 0, n)
                            }
                        }
                    }
                    val actualSha256 = digest.digest().joinToString("") { b -> "%02x".format(b) }
                    if (expectedSha256.isNotEmpty() && actualSha256 != expectedSha256) {
                        Logger.w("UpdateChecker: SHA-256 mismatch attempt ${attempt + 1}/3: expected $expectedSha256 got $actualSha256")
                        out.delete()
                        return@repeat
                    }
                    return out
                }
            } catch (e: Exception) {
                Logger.w("UpdateChecker: download attempt ${attempt + 1}/3 failed", e)
                out.delete()
            }
        }
        return null
    }

    private fun clearAll(prefs: android.content.SharedPreferences) {
        val readyPath = prefs.getString(PREF_READY_PATH, null)
        if (readyPath != null) File(readyPath).delete()
        prefs.edit().remove(PREF_READY_VERSION).remove(PREF_READY_PATH).apply()
    }

    private fun isValidVersion(s: String): Boolean = s.length >= 8 && s.all { it.isDigit() }

    companion object {
        // Version + path of the APK that's downloaded, verified and ready to
        // install. Both must be present and the file must still exist for
        // the update to be offered.
        fun readyVersion(context: Context): String? {
            val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            val version = prefs.getString(PREF_READY_VERSION, null) ?: return null
            val path = prefs.getString(PREF_READY_PATH, null) ?: return null
            if (!File(path).exists()) return null
            // Already running this version or newer -- clear stale state immediately.
            if (version <= BuildConfig.VERSION_NAME) {
                File(path).delete()
                prefs.edit().remove(PREF_READY_VERSION).remove(PREF_READY_PATH).apply()
                return null
            }
            return version
        }

        fun readyApkFile(context: Context): File? {
            val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            val path = prefs.getString(PREF_READY_PATH, null) ?: return null
            return File(path).takeIf { it.exists() }
        }
    }
}

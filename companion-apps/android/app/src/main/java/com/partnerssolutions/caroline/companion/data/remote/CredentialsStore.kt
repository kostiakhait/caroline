package com.partnerssolutions.caroline.companion.data.remote

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.partnerssolutions.caroline.companion.util.Logger

/**
 * Persists the user's SquirrelWisdom email+password (NOT the v2 session
 * token itself, which expires server-side) so the app doesn't need a
 * fresh login every process restart -- mirrors the desktop backend's own
 * choice (login_api.py's get_v2_session(): stores credentials, mints a
 * session on demand, never persists the token).
 *
 * Backed by EncryptedSharedPreferences (AES256-GCM, key in the Android
 * Keystore) rather than plain DataStore -- this is a real account
 * password, not a value that's fine to leave in a plain-text prefs file
 * even inside app-private storage.
 */
object CredentialsStore {
    private const val FILE_NAME = "companion_credentials"
    private const val KEY_EMAIL = "email"
    private const val KEY_PASSWORD = "password"

    private var prefs: SharedPreferences? = null

    fun init(context: Context) {
        try {
            val masterKey = MasterKey.Builder(context)
                .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
                .build()
            prefs = EncryptedSharedPreferences.create(
                context, FILE_NAME, masterKey,
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
            )
        } catch (exc: Exception) {
            // Keystore issues (custom ROMs, corrupted keystore) shouldn't
            // crash the app -- worst case, login just isn't persisted.
            Logger.e("CredentialsStore.init failed, login won't persist", exc)
        }
    }

    data class Credentials(val email: String, val password: String)

    fun load(): Credentials? {
        val p = prefs ?: return null
        val email = p.getString(KEY_EMAIL, null) ?: return null
        val password = p.getString(KEY_PASSWORD, null) ?: return null
        return Credentials(email, password)
    }

    fun save(email: String, password: String) {
        prefs?.edit()?.putString(KEY_EMAIL, email)?.putString(KEY_PASSWORD, password)?.apply()
    }

    fun clear() {
        prefs?.edit()?.clear()?.apply()
    }
}

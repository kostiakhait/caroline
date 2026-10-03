package com.partnerssolutions.caroline.companion.ui.login

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.partnerssolutions.caroline.companion.data.remote.CamerlengoRepository
import com.partnerssolutions.caroline.companion.util.Logger
import com.partnerssolutions.caroline.companion.util.retryOnce
import kotlinx.coroutines.launch
import java.io.IOException

class LoginViewModel(private val repository: CamerlengoRepository = CamerlengoRepository()) : ViewModel() {
    var email by mutableStateOf("")
    var password by mutableStateOf("")
    var isLoading by mutableStateOf(false)
        private set
    var error by mutableStateOf<String?>(null)
        private set

    fun login(onSuccess: () -> Unit) {
        if (email.isBlank() || password.isBlank()) {
            Logger.w("LoginViewModel.login: rejected -- email or password blank")
            error = "Enter both your SquirrelWisdom email and password."
            return
        }
        Logger.i("LoginViewModel.login: attempting for email=${email.trim()}")
        isLoading = true
        error = null
        viewModelScope.launch {
            try {
                // retryOnce: per real incident (2026-09-26) -- a raw
                // SocketTimeoutException (message literally "timeout",
                // OkHttpClient has no explicit timeouts configured) from a
                // cold-start network hiccup surfaced here verbatim, easily
                // mistaken for a problem with what was typed rather than a
                // one-off network stumble. See retryOnce's own doc comment.
                retryOnce { repository.login(email.trim(), password) }
                Logger.i("LoginViewModel.login: succeeded")
                onSuccess()
            } catch (exc: IOException) {
                // A real network failure (timeout, no connection, DNS) --
                // distinct from CamerlengoException (a clean server answer
                // like "Invalid username or password"), so say so plainly
                // instead of showing the raw "timeout"/exception text.
                Logger.w("LoginViewModel.login: network failure", exc)
                error = "Network timeout -- check your connection and try again."
            } catch (exc: Exception) {
                Logger.w("LoginViewModel.login: failed", exc)
                error = exc.message ?: "Login failed."
            } finally {
                isLoading = false
            }
        }
    }
}

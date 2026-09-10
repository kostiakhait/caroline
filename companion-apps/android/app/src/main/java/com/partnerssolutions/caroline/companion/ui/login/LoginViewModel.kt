package com.partnerssolutions.caroline.companion.ui.login

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.partnerssolutions.caroline.companion.data.remote.CamerlengoRepository
import com.partnerssolutions.caroline.companion.data.remote.SessionHolder
import kotlinx.coroutines.launch

class LoginViewModel(private val repository: CamerlengoRepository = CamerlengoRepository()) : ViewModel() {
    var email by mutableStateOf("")
    var password by mutableStateOf("")
    var isLoading by mutableStateOf(false)
        private set
    var error by mutableStateOf<String?>(null)
        private set

    fun login(onSuccess: () -> Unit) {
        if (email.isBlank() || password.isBlank()) {
            error = "Enter both your SquirrelWisdom email and password."
            return
        }
        isLoading = true
        error = null
        viewModelScope.launch {
            try {
                val session = repository.login(email.trim(), password)
                SessionHolder.set(session)
                onSuccess()
            } catch (exc: Exception) {
                error = exc.message ?: "Login failed."
            } finally {
                isLoading = false
            }
        }
    }
}

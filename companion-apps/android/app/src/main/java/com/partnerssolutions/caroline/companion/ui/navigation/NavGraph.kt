package com.partnerssolutions.caroline.companion.ui.navigation

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.navigation.NavHostController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.partnerssolutions.caroline.companion.data.remote.CamerlengoRepository
import com.partnerssolutions.caroline.companion.data.remote.CredentialsStore
import com.partnerssolutions.caroline.companion.data.remote.SessionHolder
import com.partnerssolutions.caroline.companion.ui.login.LoginScreen
import com.partnerssolutions.caroline.companion.ui.tabs.CompanionTabsScreen
import com.partnerssolutions.caroline.companion.util.Logger

private enum class AutoLoginState { CHECKING, LOGGED_IN, LOGGED_OUT }

/**
 * Silently re-logs in from CredentialsStore before deciding where to
 * land, so a process restart (killed app, phone reboot) doesn't force
 * the user to type their password again whenever it can be avoided --
 * see CamerlengoRepository/CredentialsStore/SessionHolder's own doc
 * comments for the full persisted-login design.
 */
@Composable
fun NavGraph(navController: NavHostController = rememberNavController()) {
    var autoLoginState by remember { mutableStateOf(AutoLoginState.CHECKING) }

    LaunchedEffect(Unit) {
        if (SessionHolder.session != null) {
            autoLoginState = AutoLoginState.LOGGED_IN
            return@LaunchedEffect
        }
        val creds = CredentialsStore.load()
        if (creds == null) {
            autoLoginState = AutoLoginState.LOGGED_OUT
            return@LaunchedEffect
        }
        autoLoginState = try {
            CamerlengoRepository().login(creds.email, creds.password)
            Logger.i("auto-login from stored credentials succeeded")
            AutoLoginState.LOGGED_IN
        } catch (exc: Exception) {
            Logger.w("auto-login from stored credentials failed, falling back to manual login", exc)
            AutoLoginState.LOGGED_OUT
        }
    }

    when (autoLoginState) {
        AutoLoginState.CHECKING -> Box(Modifier.fillMaxSize()) {
            CircularProgressIndicator(modifier = Modifier.align(Alignment.Center))
        }
        else -> {
            val startDestination = if (autoLoginState == AutoLoginState.LOGGED_IN) Screen.Tabs.route else Screen.Login.route
            NavHost(navController = navController, startDestination = startDestination) {
                composable(Screen.Login.route) {
                    LoginScreen(
                        onLoginSuccess = {
                            navController.navigate(Screen.Tabs.route) {
                                popUpTo(Screen.Login.route) { inclusive = true }
                            }
                        },
                    )
                }
                composable(Screen.Tabs.route) {
                    CompanionTabsScreen(
                        onLogout = {
                            navController.navigate(Screen.Login.route) {
                                popUpTo(0) { inclusive = true }
                            }
                        },
                    )
                }
            }
        }
    }
}

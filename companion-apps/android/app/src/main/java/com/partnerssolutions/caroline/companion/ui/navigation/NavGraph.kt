package com.partnerssolutions.caroline.companion.ui.navigation

import androidx.compose.runtime.Composable
import androidx.navigation.NavHostController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.partnerssolutions.caroline.companion.data.remote.SessionHolder
import com.partnerssolutions.caroline.companion.ui.login.LoginScreen
import com.partnerssolutions.caroline.companion.ui.tabs.CompanionTabsScreen

@Composable
fun NavGraph(navController: NavHostController = rememberNavController()) {
    val startDestination = if (SessionHolder.session != null) Screen.Tabs.route else Screen.Login.route

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

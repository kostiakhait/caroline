package com.partnerssolutions.caroline.companion.ui.navigation

sealed class Screen(val route: String) {
    data object Login : Screen("login")
    data object Tabs : Screen("tabs")
}

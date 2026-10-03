package com.partnerssolutions.caroline.companion.ui

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.ui.Modifier
import com.partnerssolutions.caroline.companion.ui.navigation.NavGraph
import com.partnerssolutions.caroline.companion.ui.theme.CarolineCompanionTheme
import com.partnerssolutions.caroline.companion.util.Logger

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Logger.i("MainActivity.onCreate")
        enableEdgeToEdge()
        setContent {
            CarolineCompanionTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    NavGraph()
                }
            }
        }
    }
}

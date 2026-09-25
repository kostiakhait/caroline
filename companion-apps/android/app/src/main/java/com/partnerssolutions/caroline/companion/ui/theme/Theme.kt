package com.partnerssolutions.caroline.companion.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable

/**
 * The real Caroline brand palette (Color.kt, taken directly from the
 * marketing site's CSS), forced on unconditionally -- no dynamic Material
 * You color (that would replace it with whatever's in the user's
 * wallpaper, defeating the point of having a brand at all) and no light
 * variant (the site itself has none; this is a dark-only HUD identity by
 * design, not an oversight).
 */
private val CarolineColors = darkColorScheme(
    primary = CarolineCyan,
    onPrimary = CarolineOnCyan,
    primaryContainer = CarolineCyanDim,
    onPrimaryContainer = CarolineCyanSoft,
    secondary = CarolineGold,
    onSecondary = CarolineOnCyan,
    error = CarolineRed,
    onError = CarolineText,
    background = CarolineBg,
    onBackground = CarolineText,
    surface = CarolineBgPanel,
    onSurface = CarolineText,
    surfaceVariant = CarolineBgPanel2,
    onSurfaceVariant = CarolineTextDim,
    outline = CarolineBorder,
)

@Composable
fun CarolineCompanionTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = CarolineColors, typography = Typography, content = content)
}

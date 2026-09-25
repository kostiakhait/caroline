package com.partnerssolutions.caroline.companion.ui.theme

import androidx.compose.ui.graphics.Color

/**
 * Caroline's real brand palette (2026-09-25) -- taken directly from the
 * marketing site's own CSS custom properties (caroline-site/assets/css/
 * style.css's :root block, "Iron Man / HUD visual language"), not
 * re-derived. Every name here mirrors that file's own --variable name so
 * the two stay easy to cross-check by eye.
 */
val CarolineBg = Color(0xFF05070C) // --bg
val CarolineBg1 = Color(0xFF090D15) // --bg-1
val CarolineBgPanel = Color(0xFF0B111C) // --bg-panel
val CarolineBgPanel2 = Color(0xFF0F1826) // --bg-panel-2
val CarolineCyan = Color(0xFF3DDCFF) // --cyan (primary accent)
val CarolineCyanSoft = Color(0xFF8FE9FF) // --cyan-soft
val CarolineCyanDim = Color(0xFF1C5F73) // --cyan-dim
val CarolineRed = Color(0xFFFF3B3F) // --red
val CarolineGold = Color(0xFFF0B429) // --gold
val CarolineText = Color(0xFFEEF4FB) // --text
val CarolineTextDim = Color(0xFF9FB0C3) // --text-dim
val CarolineTextFaint = Color(0xFF5D6D82) // --text-faint
// site's --border/--border-strong are semi-transparent cyan overlays, not
// solid colors -- Compose's own outline roles take a solid value, so this
// is the closest flat equivalent (matches --cyan-dim's own darkened cyan).
val CarolineBorder = Color(0xFF2A4A57)
// The site's own .btn-primary text color on a cyan button -- near-black,
// not pure black, for a touch less contrast harshness.
val CarolineOnCyan = Color(0xFF04121A)

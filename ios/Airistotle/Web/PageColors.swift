import SwiftUI
import UIKit

/// The web client's own colours (`static/css/app.css`, `:root` and its
/// `prefers-color-scheme: dark` block), for what the app draws around and behind the page so
/// it never shows a white strip or a white flash.
enum PageColors {
    /// `--bg`: the page's background.
    static let background = dynamic(light: 0xF7F5F0, dark: 0x141312)
    /// `--surface`: the header's background (the status bar area above it matches it).
    static let surface = dynamic(light: 0xFFFEFB, dark: 0x1C1B19)
    /// `--surface-2`: a raised panel (code, an answered option).
    static let surface2 = dynamic(light: 0xF0EDE5, dark: 0x252421)
    /// `--fg`, `--muted`: text and secondary text.
    static let foreground = dynamic(light: 0x1C1B18, dark: 0xECE9E3)
    static let muted = dynamic(light: 0x5C5850, dark: 0xA39E94)
    /// `--border`, `--border-strong`.
    static let border = dynamic(light: 0xE2DDD2, dark: 0x33312D)
    static let borderStrong = dynamic(light: 0xCBC4B6, dark: 0x4A4741)
    /// `--accent`, `--accent-fg`, `--accent-soft`: links, primary buttons, a chosen option.
    static let accent = dynamic(light: 0x28598A, dark: 0x8FB9E6)
    static let accentForeground = dynamic(light: 0xFFFFFF, dark: 0x0E1620)
    static let accentSoft = dynamic(light: 0xE7EEF6, dark: 0x1C2836)
    /// `--ok`/`--ok-bg`, `--warn`/`--warn-bg`, `--err`/`--err-bg`: the verdicts.
    static let ok = dynamic(light: 0x22643A, dark: 0x8FD19E)
    static let okBackground = dynamic(light: 0xE5F1E8, dark: 0x1B2D20)
    static let warn = dynamic(light: 0x7A5410, dark: 0xE7C06F)
    static let warnBackground = dynamic(light: 0xFAF0D9, dark: 0x332818)
    static let error = dynamic(light: 0xA32626, dark: 0xF2A199)
    static let errorBackground = dynamic(light: 0xFBE8E5, dark: 0x3A1E1B)

    private static func dynamic(light: UInt32, dark: UInt32) -> UIColor {
        UIColor { traits in rgb(traits.userInterfaceStyle == .dark ? dark : light) }
    }

    static func rgb(_ value: UInt32) -> UIColor {
        UIColor(
            red: CGFloat((value >> 16) & 0xFF) / 255,
            green: CGFloat((value >> 8) & 0xFF) / 255,
            blue: CGFloat(value & 0xFF) / 255,
            alpha: 1
        )
    }
}

extension Color {
    static let pageBackground = Color(uiColor: PageColors.background)
    static let pageSurface = Color(uiColor: PageColors.surface)
    static let pageSurface2 = Color(uiColor: PageColors.surface2)
    static let pageForeground = Color(uiColor: PageColors.foreground)
    static let pageMuted = Color(uiColor: PageColors.muted)
    static let pageBorder = Color(uiColor: PageColors.border)
    static let pageBorderStrong = Color(uiColor: PageColors.borderStrong)
    static let pageAccent = Color(uiColor: PageColors.accent)
    static let pageAccentForeground = Color(uiColor: PageColors.accentForeground)
    static let pageAccentSoft = Color(uiColor: PageColors.accentSoft)
    static let pageOK = Color(uiColor: PageColors.ok)
    static let pageOKBackground = Color(uiColor: PageColors.okBackground)
    static let pageWarn = Color(uiColor: PageColors.warn)
    static let pageWarnBackground = Color(uiColor: PageColors.warnBackground)
    static let pageError = Color(uiColor: PageColors.error)
    static let pageErrorBackground = Color(uiColor: PageColors.errorBackground)
}

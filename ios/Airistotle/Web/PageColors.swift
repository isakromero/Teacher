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
}

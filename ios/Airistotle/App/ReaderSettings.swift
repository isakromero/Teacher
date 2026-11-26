import Foundation
import Observation

/// The size of the web pages' text, as a percentage of the page's own size.
enum TextSize {
    static let key = "textSizePercent"
    static let defaultPercent = 100
    /// The sizes the stepper goes through.
    static let steps = [85, 90, 95, 100, 110, 120, 130, 140, 150]
    static var range: ClosedRange<Int> { steps.first! ... steps.last! } // swiftlint:disable:this force_unwrapping

    /// The step nearest to `percent` (a stored value from elsewhere is brought onto the scale).
    static func nearestStep(_ percent: Int) -> Int {
        steps.min { abs($0 - percent) < abs($1 - percent) } ?? defaultPercent
    }

    static func larger(than percent: Int) -> Int? { steps.first { $0 > percent } }
    static func smaller(than percent: Int) -> Int? { steps.last { $0 < percent } }

    /// The style that sets it: the root font size, which every `rem` size of the web client
    /// (text, spacing, touch targets) follows. Media queries keep using the browser's initial
    /// size, so the phone layout stays the phone layout and nothing gets wider than the screen.
    static func css(_ percent: Int) -> String {
        "html { font-size: \(percent)% !important; }"
    }
}

/// The reader's settings that change how pages are shown (UserDefaults).
@MainActor
@Observable
final class ReaderSettings {
    static let keepAwakeKey = "keepScreenAwakeOnStudyPages"

    @ObservationIgnored private let defaults: UserDefaults

    /// The web pages' text size, in percent (one of `TextSize.steps`).
    private(set) var textSizePercent: Int

    /// Set the text size (brought onto `TextSize.steps`) and keep it.
    func setTextSize(_ percent: Int) {
        let step = TextSize.nearestStep(percent)
        textSizePercent = step
        defaults.set(step, forKey: TextSize.key)
    }

    /// Keep the screen on while a study page is open (default on).
    var keepScreenAwake: Bool {
        didSet { defaults.set(keepScreenAwake, forKey: Self.keepAwakeKey) }
    }

    init(defaults: UserDefaults) {
        self.defaults = defaults
        let stored = defaults.object(forKey: TextSize.key) as? Int
        textSizePercent = stored.map(TextSize.nearestStep) ?? TextSize.defaultPercent
        keepScreenAwake = defaults.object(forKey: Self.keepAwakeKey) as? Bool ?? true
    }
}

/// Whether the idle timer is turned off: only while a study page (`/artifacts/{slug}`) is on
/// screen, the app is in the foreground, someone is signed in and the setting is on.
enum ScreenAwakePolicy {
    static func isStudyPage(_ target: String?) -> Bool {
        guard let target else { return false }
        return OfflineService.pageSlug(String(target.prefix { $0 != "?" && $0 != "#" })) != nil
    }

    static func keepAwake(enabled: Bool, target: String?, appActive: Bool, signedIn: Bool) -> Bool {
        enabled && appActive && signedIn && isStudyPage(target)
    }
}

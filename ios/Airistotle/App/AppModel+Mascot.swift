import Foundation

/// Aristotle's part of the app's state (`MascotCenter`).
extension AppModel {
    /// Welcome the reader back if the app was last active long enough ago (`WelcomeBack`).
    func welcomeBack(lastActive: Date?) {
        guard case .signedIn = phase else { return }
        mascot.welcomeBack(lastActive: lastActive, signedIn: true, snapshot: habits.snapshot)
    }

    #if DEBUG
    /// UI tests and screenshots (`-AiristotleLastActiveDaysAgo <days>`): as if the app had last
    /// been active that long ago, then the welcome back as on coming back.
    func welcomeBackForUITests(_ arguments: [String]) {
        guard let index = arguments.firstIndex(of: "-AiristotleLastActiveDaysAgo"), index + 1 < arguments.count,
              let days = Double(arguments[index + 1]) else { return }
        mascot.store.setLastActive(Date().addingTimeInterval(-days * 86_400))
        welcomeBack(lastActive: mascot.noteActive())
    }
    #endif
}

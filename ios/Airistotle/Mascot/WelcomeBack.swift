import Foundation

/// Whether to welcome the reader back when the app becomes active, and with what.
enum WelcomeBack {
    /// Away at least this long (since the app was last active) earns a welcome.
    static let threshold: TimeInterval = 3 * 24 * 3600
    /// The welcome waits this long after the app became active, so it comes once the screen
    /// has settled rather than during launch.
    static let settle: TimeInterval = 1.5

    /// The welcome, or nil: signed out, never active before (the first launch after install or
    /// signing in), back sooner than `threshold`, or a clock that went backwards. The message
    /// says what is due now (from the widget's snapshot), or offers the page to resume.
    static func message(lastActive: Date?, now: Date, signedIn: Bool, snapshot: WidgetSnapshot?) -> MascotMessage? {
        guard signedIn, let lastActive, now.timeIntervalSince(lastActive) >= threshold else { return nil }
        guard let snapshot, snapshot.signedIn else { return MascotText.welcomeBack(due: nil, atLeast: false, resumeTitle: nil) }
        var due: Int?
        var atLeast = false
        if let reviews = snapshot.reviews {
            let moment = ReviewProjection.moment(reviews, habit: snapshot.habit, at: now)
            due = moment.state.dueCount
            if case .due(_, let more) = moment.state { atLeast = more }
        }
        return MascotText.welcomeBack(due: due, atLeast: atLeast, resumeTitle: snapshot.continueTitle)
    }
}

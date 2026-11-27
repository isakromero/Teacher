import Foundation

/// A review session the app opens over the page.
struct ReviewRequest: Identifiable, Equatable {
    enum Kind: Equatable {
        /// Everything due (today's cap).
        case all
        /// Time-boxed: questions until `minutes` are up (the one on screen is always finished).
        case quick(minutes: Int)
    }

    enum Reason: Equatable {
        case chosen
        /// The web review page was opened while the server cannot be reached.
        case webPageOffline
    }

    /// The web client's review page.
    static let webPath = "/review"

    let id = UUID()
    var kind: Kind
    var concept: String?
    var reason: Reason = .chosen
}

/// A quick review's time box: how long is left, and whether the session goes on after a verdict.
struct TimeBox: Equatable, Sendable {
    var start: Date
    var duration: TimeInterval

    init(start: Date, minutes: Int) {
        self.start = start
        duration = TimeInterval(minutes * 60)
    }

    func remaining(at now: Date) -> TimeInterval { max(0, start.addingTimeInterval(duration).timeIntervalSince(now)) }
    func isOver(at now: Date) -> Bool { remaining(at: now) <= 0 }
    /// How much of the time is used, 0...1.
    func used(at now: Date) -> Double { duration > 0 ? min(1, max(0, now.timeIntervalSince(start) / duration)) : 1 }

    /// "4:05": minutes and seconds left, rounded up (so it reads 0:00 only when time is up).
    func label(at now: Date) -> String {
        let seconds = Int(remaining(at: now).rounded(.up))
        return String(format: "%d:%02d", seconds / 60, seconds % 60)
    }

    /// The spoken form: "4 minutes 5 seconds left".
    func spoken(at now: Date) -> String {
        let seconds = Int(remaining(at: now).rounded(.up))
        if seconds == 0 { return "Time is up" }
        let minutes = seconds / 60
        let parts = [minutes > 0 ? "\(minutes) minute\(minutes == 1 ? "" : "s")" : nil,
                     seconds % 60 > 0 ? "\(seconds % 60) second\(seconds % 60 == 1 ? "" : "s")" : nil]
        return parts.compactMap(\.self).joined(separator: " ") + " left"
    }
}

/// What happens after a verdict: another question, or the summary.
enum ReviewStep: Equatable {
    case next
    case finish

    /// Time is never cut off mid-answer: the box is checked only once a verdict is shown.
    static func afterVerdict(timeBox: TimeBox?, now: Date, moreLeft: Bool) -> ReviewStep {
        guard moreLeft else { return .finish }
        if let timeBox, timeBox.isOver(at: now) { return .finish }
        return .next
    }
}

/// The review page's wording (`static/js/review_model.js`), so the native screen says what the
/// web page says.
enum ReviewText {
    private static func plural(_ count: Int, _ one: String, _ many: String? = nil) -> String {
        "\(count) \(count == 1 ? one : many ?? one + "s")"
    }

    /// `dueIn`: "now", "in 40 minutes", "in 5 hours", "tomorrow", "in 3 days", "in 4 weeks".
    static func dueIn(_ when: Date, now: Date) -> String {
        let millis = when.timeIntervalSince(now) * 1000
        if millis <= 0 { return "now" }
        let minutes = Int((millis / 60000).rounded())
        if minutes < 60 { return "in \(plural(max(minutes, 1), "minute"))" }
        let hours = Int((millis / 3_600_000).rounded())
        if hours < 24 { return "in \(plural(hours, "hour"))" }
        let days = Int((millis / 86_400_000).rounded())
        if days == 1 { return "tomorrow" }
        if days < 28 { return "in \(days) days" }
        return "in \(Int((Double(days) / 7).rounded())) weeks"
    }

    /// `formatWhen`: "Thu 26 Nov, 4:05 PM" in the reader's locale.
    static func when(_ date: Date) -> String {
        date.formatted(.dateTime.weekday(.abbreviated).day().month(.abbreviated).hour().minute())
    }

    /// `intervalText`: "1 day", "2.2 days", "3 weeks".
    static func interval(_ days: Double) -> String {
        if days >= 14 { return plural(Int((days / 7).rounded()), "week") }
        let rounded = (days * 10).rounded() / 10
        let text = rounded == rounded.rounded() ? String(Int(rounded)) : String(rounded)
        return "\(text) \(rounded == 1 ? "day" : "days")"
    }

    /// `scheduleText`: the line under a graded review.
    static func schedule(verdict: String, intervalDays: Double) -> String {
        let when = interval(intervalDays)
        switch verdict {
        case "passed": return "Right. This question comes back in \(when), a longer gap than last time."
        case "partial": return "Partly right. This question comes back in \(when), the same gap as last time."
        default: return "Not this time. This question comes back in \(when), so you can try it again soon."
        }
    }

    static func verdictTitle(_ verdict: String) -> String {
        switch verdict {
        case "passed": "Right"
        case "partial": "Partly right"
        default: "Not this time"
        }
    }

    // swiftlint:disable function_parameter_count
    /// When nothing can be asked now: why, from the queue's numbers (`emptyState`).
    static func nothingDue(itemsTotal: Int, due: Int, remainingToday: Int, cap: Int, nextDue: Date?, now: Date) -> String {
        if itemsTotal == 0 {
            return "Nothing to review yet. A question joins the review queue once you have answered it on a study page."
        }
        if due > 0 && remainingToday == 0 {
            return "You have done today's \(cap) reviews. \(plural(due, "more question is", "more questions are")) due: "
                + "stop here, or keep going."
        }
        guard let nextDue else { return "Nothing is due now, and no question is scheduled yet." }
        return "Nothing is due now. The next question is due \(dueIn(nextDue, now: now)) (\(when(nextDue)))."
    }
    // swiftlint:enable function_parameter_count
}

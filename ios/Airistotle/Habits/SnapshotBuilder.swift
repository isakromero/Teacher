import Foundation

/// Builds the widget's snapshot from what the app knows: the server's summary, the review
/// questions saved on the phone and the answers made here that the server does not count yet.
enum SnapshotBuilder {
    struct Inputs: Sendable {
        var now: Date
        var goal: Int
        /// The last summary read from the server (kept on the phone), if any.
        var summary: PhoneSummary?
        /// `summary` was read from the server in this refresh; otherwise the server could not be
        /// asked and the review numbers come from the questions saved on the phone.
        var summaryIsCurrent: Bool
        var queue: LocalReviewQueue?
        /// When each answer the summary does not count yet was made (`localAnswers`).
        var localAnswers: [Date]
        var zone: TimeZone
    }

    static func snapshot(_ inputs: Inputs) -> WidgetSnapshot {
        WidgetSnapshot(writtenAt: inputs.now, signedIn: true, reviews: reviews(inputs), habit: habit(inputs),
                       continueTitle: inputs.summary?.resume?.title)
    }

    /// The review numbers: the server's when it was just asked; offline, the saved questions on
    /// the phone's clock (they include the answers made offline); failing both, the last summary
    /// (the widget then projects it and says how old it is).
    static func reviews(_ inputs: Inputs) -> WidgetSnapshot.Reviews? {
        if inputs.summaryIsCurrent, let summary = inputs.summary {
            return fromSummary(summary)
        }
        if let queue = inputs.queue {
            let now = Instant(inputs.now)
            let remaining = queue.remaining(now: now)
            return WidgetSnapshot.Reviews(
                computedAt: inputs.now, asOf: queue.bundle.bundledAt.date, source: .phone, timeZone: queue.bundle.timeZone.identifier,
                dueNow: queue.due(now: now).count, cap: queue.bundle.cap, remainingToday: remaining,
                doneToday: max(queue.bundle.cap - remaining, 0),
                upcoming: queue.upcoming(after: now).map(\.date), upcomingComplete: true)
        }
        return inputs.summary.map(fromSummary)
    }

    private static func fromSummary(_ summary: PhoneSummary) -> WidgetSnapshot.Reviews {
        let now = summary.now
        let upcoming = summary.upcoming.filter { $0 > now }.map(\.date)
        return WidgetSnapshot.Reviews(
            computedAt: now.date, asOf: now.date, source: .server, timeZone: summary.zone.identifier, dueNow: summary.dueNow,
            cap: summary.cap, remainingToday: summary.remainingToday, doneToday: summary.doneToday, upcoming: upcoming,
            upcomingComplete: summary.upcomingTotal <= summary.upcoming.count)
    }

    /// Answers per day: the server's counts plus the answers made on the phone that it does not
    /// count yet, on the day they were made in the summary's zone. Nil before any summary.
    static func habit(_ inputs: Inputs) -> WidgetSnapshot.Habit? {
        guard let summary = inputs.summary else { return nil }
        var counts = Dictionary(summary.days.map { ($0.day, $0.total) }, uniquingKeysWith: +)
        for (day, extra) in HabitMath.count(inputs.localAnswers, in: summary.zone) { counts[day, default: 0] += extra }
        let days = counts.filter { $0.value > 0 }.map { WidgetSnapshot.DayCount(day: $0.key, answers: $0.value) }.sorted { $0.day < $1.day }
        return WidgetSnapshot.Habit(goal: inputs.goal, timeZone: summary.zone.identifier, firstDay: summary.firstDay, days: days,
                                    asOf: summary.now.date)
    }

    /// The answers made on this phone that the summary taken at `summaryTime` does not count:
    /// every study-page and review answer still waiting to upload (whenever it was made), and
    /// the reviews answered online here after the summary was taken.
    static func localAnswers(pageAttempts: [Instant], reviewsWaiting: [Instant], reviewsAnswered: [LocalReviewAnswer],
                             summaryTime: Instant?) -> [Date] {
        let answeredSince = reviewsAnswered.filter { answer in
            guard !answer.pending else { return false } // counted with those waiting
            guard let summaryTime else { return true }
            return answer.madeAt > summaryTime
        }
        return (pageAttempts + reviewsWaiting + answeredSince.map(\.madeAt)).map(\.date).sorted()
    }
}

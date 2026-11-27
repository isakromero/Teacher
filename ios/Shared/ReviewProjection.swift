import Foundation

/// Where the review queue stands at a moment, projected from a snapshot.
enum ReviewState: Equatable, Sendable {
    /// Questions to answer now (as many as today's cap still allows). `atLeast`: more may be
    /// due than the snapshot lists.
    case due(Int, atLeast: Bool)
    /// Nothing more to do today: the reviews due were done (or today's cap is reached, then
    /// `next` is tomorrow's start), or today's goal is met with nothing due.
    case doneToday(next: Date?)
    /// Nothing due, and nothing done today; `next` is when the next question comes due.
    case nothingDue(next: Date?)

    var dueCount: Int {
        if case .due(let count, _) = self { count } else { 0 }
    }
}

/// The review queue and goal at one moment, as the widget, the reminders and the "Reviews due"
/// shortcut show it.
struct ReviewMoment: Equatable, Sendable {
    var date: Date
    var state: ReviewState
    var goal: GoalStatus?
    /// The numbers are older than `WidgetSnapshot.staleAfter`: say when they are from.
    var stale: Bool
    var asOf: Date
    var source: WidgetSnapshot.Source
    var zone: TimeZone
}

/// What the widget shows.
enum WidgetDisplay: Equatable, Sendable {
    /// The shared data cannot be read (the App Group is missing, or the file is damaged).
    case unavailable(String)
    case signedOut
    /// Signed in, but the app has not loaded the review queue yet.
    case noData
    case reviews(ReviewMoment, continueTitle: String?)
}

/// The widget's timeline: because the snapshot lists when further questions come due, the
/// count rises at those moments without the app running; at midnight (the review day's zone) the
/// day's cap and goal start again.
enum ReviewProjection {
    /// The review queue at `date` (not before the snapshot's own moment).
    static func moment(_ reviews: WidgetSnapshot.Reviews, habit: WidgetSnapshot.Habit?, at date: Date) -> ReviewMoment {
        let zone = reviews.zone
        let moment = max(date, reviews.computedAt)
        let sameDay = LocalDay(moment, in: zone) == LocalDay(reviews.computedAt, in: zone)
        let cameDue = reviews.upcoming.prefix { $0 <= moment }.count
        let due = reviews.dueNow + cameDue
        let remaining = sameDay ? reviews.remainingToday : reviews.cap
        let done = sameDay ? reviews.doneToday : 0
        let askable = min(due, remaining)
        let next = reviews.upcoming.first { $0 > moment }
        let goal = habit.map { HabitMath.status($0, at: moment) }
        let pastList = reviews.upcoming.last.map { moment >= $0 } ?? true
        let state: ReviewState
        if askable > 0 {
            state = .due(askable, atLeast: !reviews.upcomingComplete && pastList && askable < remaining)
        } else if due > 0 {
            state = .doneToday(next: LocalDay(moment, in: zone).adding(days: 1).start(in: zone))
        } else if done > 0 || goal?.met == true {
            state = .doneToday(next: next)
        } else {
            state = .nothingDue(next: next)
        }
        return ReviewMoment(date: date, state: state, goal: goal, stale: date.timeIntervalSince(reviews.asOf) > WidgetSnapshot.staleAfter,
                            asOf: reviews.asOf, source: reviews.source, zone: zone)
    }

    /// What to show at `date` from what the widget could read.
    static func display(_ read: Result<WidgetSnapshot?, SharedContainer.Failure>, at date: Date) -> WidgetDisplay {
        switch read {
        case .failure(let failure):
            return .unavailable(failure.localizedDescription)
        case .success(nil):
            return .noData
        case .success(let snapshot?):
            guard snapshot.signedIn else { return .signedOut }
            guard let reviews = snapshot.reviews else { return .noData }
            return .reviews(moment(reviews, habit: snapshot.habit, at: date), continueTitle: snapshot.continueTitle)
        }
    }

    static let horizon: TimeInterval = 48 * 3600
    static let maxEntries = 120

    /// The moments the display changes after `now`, within the horizon: each due time, each
    /// midnight, and the moment the numbers become stale.
    static func changeDates(_ snapshot: WidgetSnapshot, from now: Date) -> [Date] {
        guard snapshot.signedIn, let reviews = snapshot.reviews else { return [] }
        let end = now.addingTimeInterval(horizon)
        var dates = Set(reviews.upcoming.filter { $0 > now && $0 <= end })
        var day = LocalDay(now, in: reviews.zone)
        for _ in 0..<3 {
            day = day.adding(days: 1)
            let midnight = day.start(in: reviews.zone)
            if midnight > now && midnight <= end { dates.insert(midnight) }
        }
        if let habit = snapshot.habit, habit.timeZone != reviews.timeZone {
            var goalDay = LocalDay(now, in: habit.zone)
            for _ in 0..<3 {
                goalDay = goalDay.adding(days: 1)
                let midnight = goalDay.start(in: habit.zone)
                if midnight > now && midnight <= end { dates.insert(midnight) }
            }
        }
        let stale = reviews.asOf.addingTimeInterval(WidgetSnapshot.staleAfter + 1)
        if stale > now && stale <= end { dates.insert(stale) }
        return dates.sorted()
    }

    /// Timeline entries from `now`: one now, then one at each change (at most `maxEntries`,
    /// equal displays merged), and when to ask for a new timeline (nil: only when the app
    /// writes a new snapshot).
    static func timeline(_ read: Result<WidgetSnapshot?, SharedContainer.Failure>, now: Date)
        -> (entries: [(Date, WidgetDisplay)], reloadAt: Date?) {
        var entries = [(now, display(read, at: now))]
        guard case .success(let snapshot?) = read else { return (entries, nil) }
        let dates = changeDates(snapshot, from: now)
        for date in dates.prefix(maxEntries - 1) {
            let shown = display(read, at: date)
            if let last = entries.last?.1, Self.sameShown(last, shown) { continue }
            entries.append((date, shown))
        }
        guard !dates.isEmpty else { return (entries, snapshot.signedIn && snapshot.reviews != nil ? now.addingTimeInterval(horizon) : nil) }
        let last = dates.count >= maxEntries ? dates[maxEntries - 2] : now.addingTimeInterval(horizon)
        return (entries, last)
    }

    /// The same thing on screen (the moment's own date aside).
    private static func sameShown(_ lhs: WidgetDisplay, _ rhs: WidgetDisplay) -> Bool {
        guard case .reviews(var left, let leftContinue) = lhs, case .reviews(let right, let rightContinue) = rhs else { return lhs == rhs }
        left.date = right.date
        return left == right && leftContinue == rightContinue
    }
}

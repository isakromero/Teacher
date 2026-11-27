import Foundation

/// One local notification to schedule: "7 reviews due" at the reader's chosen time on `day`.
struct PlannedReminder: Equatable, Sendable {
    static let identifierPrefix = "dev.airistotle.reminder."

    var day: LocalDay
    var fireDate: Date
    var count: Int
    var title: String
    var body: String
    /// The app icon's badge when it is delivered (nil: the badge is off).
    var badge: Int?

    var identifier: String { Self.identifierPrefix + day.description }
}

/// Decides which reminders to schedule. Each day at the chosen time, a reminder goes only if
/// reviews will be due then (projected from the snapshot's upcoming due times and the day's
/// cap); a day with nothing due gets none, so when today's reviews are all done today's goes.
/// The plan is made again whenever the numbers change, so what is scheduled ahead follows.
enum ReminderPlanner {
    static let days = 7
    static let defaultMinutes = 18 * 60

    struct Inputs: Sendable {
        var now: Date
        /// The reader's zone (the chosen time is a time of day there).
        var zone: TimeZone
        /// Minutes after midnight.
        var minutes: Int
        var reviews: WidgetSnapshot.Reviews
        var habit: WidgetSnapshot.Habit?
        var badge: Bool
    }

    static func plan(_ inputs: Inputs) -> [PlannedReminder] {
        let today = LocalDay(inputs.now, in: inputs.zone)
        var out: [PlannedReminder] = []
        for offset in 0..<days {
            let day = today.adding(days: offset)
            guard let fire = fireDate(day, minutes: inputs.minutes, zone: inputs.zone), fire > inputs.now else { continue }
            let moment = ReviewProjection.moment(inputs.reviews, habit: inputs.habit, at: fire)
            guard case .due(let count, let atLeast) = moment.state, count > 0 else { continue }
            // The streak is mentioned only today: later days' goals are not known yet.
            let goal = offset == 0 ? inputs.habit.map { HabitMath.status($0, at: inputs.now) } : nil
            out.append(PlannedReminder(day: day, fireDate: fire, count: count, title: WidgetText.due(count, atLeast: atLeast),
                                       body: body(count: count, goal: goal), badge: inputs.badge ? count : nil))
        }
        return out
    }

    /// The chosen time on `day` in `zone`; a time that does not exist that day (the clocks go
    /// forward) is the first moment after it.
    static func fireDate(_ day: LocalDay, minutes: Int, zone: TimeZone) -> Date? {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let parts = DateComponents(year: day.year, month: day.month, day: day.day, hour: minutes / 60, minute: minutes % 60)
        guard let date = calendar.date(from: parts) else { return nil }
        // A skipped hour comes out on the next hour; an hour that repeats takes the first.
        return date
    }

    /// The text under the count: the streak, plainly, while today's goal is not met.
    static func body(count: Int, goal: GoalStatus?) -> String {
        let start = count == 1 ? "A review question is ready." : "\(count) review questions are ready."
        guard let goal, !goal.met, goal.streak > 0 else { return start + " Tap to start the review." }
        let days = goal.streak == 1 ? "1 day" : "\(goal.streak) days"
        let answers = goal.left == 1 ? "1 more answer" : "\(goal.left) more answers"
        return start + " Your streak is \(days); \(answers) today meet your goal of \(goal.goal)."
    }
}

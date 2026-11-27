import Foundation

/// The words for review counts, the goal and how fresh the numbers are, shared by the widget,
/// the reminders and the Shortcuts, so they all say the same thing.
enum WidgetText {
    static let signedOut = "Open Airistotle to sign in"
    static let noData = "Open Airistotle to load your reviews"
    static let unavailableTitle = "Can't read Airistotle's data"

    static func reviews(_ count: Int, atLeast: Bool = false) -> String {
        "\(count)\(atLeast ? "+" : "") \(count == 1 && !atLeast ? "review" : "reviews")"
    }

    /// "7 reviews due".
    static func due(_ count: Int, atLeast: Bool = false) -> String {
        "\(reviews(count, atLeast: atLeast)) due"
    }

    /// A time the widget shows: "21:14" today, "tomorrow 09:00", "Tue 09:00", "3 Dec" further off.
    static func when(_ date: Date, from now: Date, zone: TimeZone) -> String {
        let days = LocalDay(now, in: zone).days(to: LocalDay(date, in: zone))
        var time = Date.FormatStyle(date: .omitted, time: .shortened)
        time.timeZone = zone
        switch days {
        case ...0: return date.formatted(time)
        case 1: return "tomorrow \(date.formatted(time))"
        case 2...6:
            var weekday = Date.FormatStyle().weekday(.abbreviated)
            weekday.timeZone = zone
            return "\(date.formatted(weekday)) \(date.formatted(time))"
        default:
            var day = Date.FormatStyle().day().month(.abbreviated)
            day.timeZone = zone
            return date.formatted(day)
        }
    }

    /// The headline and the line under it for a review state.
    static func headline(_ moment: ReviewMoment) -> (title: String, detail: String?) {
        switch moment.state {
        case .due(let count, let atLeast):
            return (due(count, atLeast: atLeast), nil)
        case .doneToday(let next):
            guard let next else { return ("All done today", "Nothing else is scheduled") }
            let tomorrow = LocalDay(moment.date, in: moment.zone).adding(days: 1).start(in: moment.zone)
            if next == tomorrow { return ("All done today", "More due tomorrow") }
            return ("All done today", "Next: \(when(next, from: moment.date, zone: moment.zone))")
        case .nothingDue(let next):
            guard let next else { return ("Nothing due", "No reviews scheduled") }
            return ("Nothing due", "until \(when(next, from: moment.date, zone: moment.zone))")
        }
    }

    /// "6 of 10 today"; once met, "Goal met: 12 today".
    static func goal(_ status: GoalStatus) -> String {
        status.met ? "Goal met: \(status.answersToday) today" : "\(status.answersToday) of \(status.goal) today"
    }

    /// "Streak 4 days" (nil without one).
    static func streak(_ status: GoalStatus) -> String? {
        status.streak > 0 ? "Streak \(status.streak) \(status.streak == 1 ? "day" : "days")" : nil
    }

    /// "Updated 14:05" / "Updated Tue 14:05" when the numbers are stale (nil when not).
    static func asOf(_ moment: ReviewMoment) -> String? {
        guard moment.stale else { return nil }
        return "Updated \(updated(moment.asOf, from: moment.date, zone: moment.zone))"
    }

    static func updated(_ date: Date, from now: Date, zone: TimeZone) -> String {
        let days = LocalDay(date, in: zone).days(to: LocalDay(now, in: zone))
        var time = Date.FormatStyle(date: .omitted, time: .shortened)
        time.timeZone = zone
        switch days {
        case ...0: return date.formatted(time)
        case 1: return "yesterday \(date.formatted(time))"
        case 2...6:
            var weekday = Date.FormatStyle().weekday(.abbreviated)
            weekday.timeZone = zone
            return "\(date.formatted(weekday)) \(date.formatted(time))"
        default:
            var day = Date.FormatStyle().day().month(.abbreviated)
            day.timeZone = zone
            return date.formatted(day)
        }
    }
}

import Foundation

/// Today's goal and the streak, as the phone works them out from answers per day.
struct GoalStatus: Equatable, Sendable {
    var today: LocalDay
    /// Study-page and review answers today (with those made on the phone and not uploaded yet).
    var answersToday: Int
    var goal: Int
    /// Consecutive days meeting the goal, ending today, or yesterday while today's is not met.
    var streak: Int
    /// The longest run of days meeting the goal within the known window.
    var best: Int
    /// Days the counts cover (from the window's first day to today).
    var windowDays: Int
    /// When the server's counts were read.
    var asOf: Date

    var met: Bool { answersToday >= goal }
    var left: Int { max(goal - answersToday, 0) }
    /// 0...1 for a progress ring.
    var fraction: Double { goal > 0 ? min(Double(answersToday) / Double(goal), 1) : 1 }
}

/// The daily goal and streak. A day meets the goal when its answers (study-page answers and
/// review answers, the summary's `total`) reach it. The server's own `streak_days` counts days
/// with any answer; this one counts days that meet the reader's goal, so it is worked out here.
enum HabitMath {
    static let goalChoices = [5, 10, 20, 30]
    static let defaultGoal = 10

    /// The status on the day `date` falls on in the habit's zone.
    static func status(_ habit: WidgetSnapshot.Habit, at date: Date) -> GoalStatus {
        status(answers: habit.answersByDay, firstDay: habit.firstDay, today: LocalDay(date, in: habit.zone), goal: habit.goal,
               asOf: habit.asOf)
    }

    static func status(answers: [LocalDay: Int], firstDay: LocalDay, today: LocalDay, goal: Int, asOf: Date) -> GoalStatus {
        let goal = max(goal, 1)
        let first = min(firstDay, today)
        return GoalStatus(today: today, answersToday: answers[today] ?? 0, goal: goal,
                          streak: streak(answers: answers, firstDay: first, today: today, goal: goal),
                          best: best(answers: answers, firstDay: first, today: today, goal: goal),
                          windowDays: first.days(to: today) + 1, asOf: asOf)
    }

    /// Consecutive days meeting `goal` ending `today`, or ending yesterday while today's goal is
    /// not met yet; days before `firstDay` are not known and end it.
    static func streak(answers: [LocalDay: Int], firstDay: LocalDay, today: LocalDay, goal: Int) -> Int {
        var day = (answers[today] ?? 0) >= goal ? today : today.adding(days: -1)
        var count = 0
        while day >= firstDay, (answers[day] ?? 0) >= goal {
            count += 1
            day = day.adding(days: -1)
        }
        return count
    }

    /// The longest run of days meeting `goal` from `firstDay` to `today`.
    static func best(answers: [LocalDay: Int], firstDay: LocalDay, today: LocalDay, goal: Int) -> Int {
        var best = 0
        var run = 0
        var day = firstDay
        while day <= today {
            run = (answers[day] ?? 0) >= goal ? run + 1 : 0
            best = max(best, run)
            day = day.adding(days: 1)
        }
        return best
    }

    /// Answers per day in `zone` for moments (answers made on the phone).
    static func count(_ moments: [Date], in zone: TimeZone) -> [LocalDay: Int] {
        var out: [LocalDay: Int] = [:]
        for moment in moments { out[LocalDay(moment, in: zone), default: 0] += 1 }
        return out
    }
}

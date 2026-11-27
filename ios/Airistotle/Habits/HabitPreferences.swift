import Foundation

/// The daily goal and the reminder settings (UserDefaults; nothing secret).
struct HabitPreferences: @unchecked Sendable {
    let defaults: UserDefaults

    static let goalKey = "dailyGoalAnswers"
    static let remindersKey = "reviewRemindersOn"
    static let reminderMinutesKey = "reviewReminderMinutes"
    static let badgeKey = "reviewBadgeOn"
    static let acknowledgedKey = "dailyGoalAcknowledgedDay"
    static let spotlightKey = "spotlightIndexed"
    static var allKeys: [String] { [goalKey, remindersKey, reminderMinutesKey, badgeKey, acknowledgedKey, spotlightKey] }

    /// Answers per day (5, 10, 20 or 30; default 10).
    var goal: Int {
        get {
            let stored = defaults.object(forKey: Self.goalKey) as? Int
            return stored.flatMap { HabitMath.goalChoices.contains($0) ? $0 : nil } ?? HabitMath.defaultGoal
        }
        nonmutating set { defaults.set(newValue, forKey: Self.goalKey) }
    }

    /// "Remind me when reviews are due" (default off).
    var remindersOn: Bool {
        get { defaults.bool(forKey: Self.remindersKey) }
        nonmutating set { defaults.set(newValue, forKey: Self.remindersKey) }
    }

    /// The reminder's time of day, in minutes after midnight (default 18:00).
    var reminderMinutes: Int {
        get {
            let stored = defaults.object(forKey: Self.reminderMinutesKey) as? Int
            return stored.flatMap { (0..<1440).contains($0) ? $0 : nil } ?? ReminderPlanner.defaultMinutes
        }
        nonmutating set { defaults.set(newValue, forKey: Self.reminderMinutesKey) }
    }

    /// "Show due count on the app icon" (default off).
    var badgeOn: Bool {
        get { defaults.bool(forKey: Self.badgeKey) }
        nonmutating set { defaults.set(newValue, forKey: Self.badgeKey) }
    }

    /// The day the goal was last acknowledged ("Today's goal is met"), once a day.
    var acknowledgedDay: LocalDay? {
        get { defaults.string(forKey: Self.acknowledgedKey).flatMap(LocalDay.init) }
        nonmutating set { defaults.set(newValue?.description, forKey: Self.acknowledgedKey) }
    }
}

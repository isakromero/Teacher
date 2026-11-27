import SwiftUI
import UIKit

/// Settings: the daily goal.
struct DailyGoalSection: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var habits = model.habits
        Section {
            Picker("Daily goal", selection: $habits.goal) {
                ForEach(HabitMath.goalChoices, id: \.self) { Text("\($0) answers").tag($0) }
            }
            .accessibilityIdentifier("settings.dailyGoal")
            if let status = habits.goalStatus {
                LabeledContent("Today", value: "\(status.answersToday) of \(status.goal)")
                    .accessibilityIdentifier("settings.goalToday")
                LabeledContent("Streak", value: status.streak > 0 ? HabitText.days(status.streak) : "None yet")
                    .accessibilityIdentifier("settings.streak")
            }
            if let problem = habits.localProblem {
                Label(problem, systemImage: "exclamationmark.triangle").foregroundStyle(.orange).font(.footnote)
            }
        } header: {
            Text("Daily goal")
        } footer: {
            Text("Answers on study pages and in reviews both count, including those made offline. A day meets the goal when "
                + "its answers reach it; the streak is the run of such days up to today (or yesterday, until today's is met).")
        }
    }
}

/// Settings: whether Aristotle speaks up (a finished page, today's goal, welcome back).
struct MascotSection: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var mascot = model.mascot
        Section {
            Toggle(isOn: $mascot.enabled) {
                HStack(spacing: 10) {
                    MascotTile(pose: .idle, pixel: 1)
                        .accessibilityHidden(true)
                    Text("Show Aristotle")
                }
            }
            .accessibilityIdentifier("settings.mascot")
            if let problem = mascot.problem {
                VStack(alignment: .leading, spacing: 6) {
                    Label(problem, systemImage: "exclamationmark.triangle").foregroundStyle(.orange).font(.footnote)
                    Button("Start his memory afresh") { mascot.forgetUnreadable() }
                        .font(.footnote)
                        .accessibilityIdentifier("settings.mascotForget")
                }
            }
        } header: {
            Text("Aristotle")
        } footer: {
            Text("A few words now and then: when you finish a study page, when today's goal is met, and when you come back "
                + "after a few days away. He never covers what you are doing and goes after a few seconds. Turned off, "
                + "today's goal is still acknowledged with a short note.")
        }
    }
}

/// Settings: reminders, the badge, and whether the widget gets its data.
struct RemindersSection: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openURL) private var openURL

    var body: some View {
        let habits = model.habits
        Section {
            Toggle("Remind me when reviews are due", isOn: Binding(
                get: { habits.remindersOn },
                set: { enabled in Task { await habits.setReminders(enabled) } }))
                .accessibilityIdentifier("settings.reminders")
            if habits.reminderAuthorization == .denied {
                VStack(alignment: .leading, spacing: 6) {
                    Label("Notifications are turned off for Airistotle in iOS Settings, so no reminder can be shown.",
                          systemImage: "bell.slash")
                        .foregroundStyle(.orange)
                        .font(.footnote)
                    Button("Open iOS Settings") {
                        if let url = URL(string: UIApplication.openNotificationSettingsURLString) { openURL(url) }
                    }
                    .accessibilityIdentifier("settings.openNotificationSettings")
                }
                .accessibilityIdentifier("settings.remindersDenied")
            }
            if habits.remindersOn {
                DatePicker("Time", selection: Binding(
                    get: { Self.date(minutes: habits.reminderMinutes) },
                    set: { habits.reminderMinutes = Self.minutes(of: $0) }), displayedComponents: .hourAndMinute)
                    .accessibilityIdentifier("settings.reminderTime")
                LabeledContent("Next reminder", value: nextText(habits))
                    .accessibilityIdentifier("settings.nextReminder")
            }
            Toggle("Show due count on the app icon", isOn: Binding(
                get: { habits.badgeOn },
                set: { enabled in Task { await habits.setBadge(enabled) } }))
                .accessibilityIdentifier("settings.badge")
            if let problem = habits.reminderProblem {
                Label(problem, systemImage: "exclamationmark.triangle").foregroundStyle(.orange).font(.footnote)
            }
            LabeledContent("Widget", value: widgetText(habits))
                .accessibilityIdentifier("settings.widget")
            ForEach([habits.widgetProblem, habits.fetchProblem, habits.backgroundProblem].compactMap(\.self), id: \.self) { problem in
                Label(problem, systemImage: "exclamationmark.triangle").foregroundStyle(.orange).font(.footnote)
            }
        } header: {
            Text("Reminders")
        } footer: {
            Text("A reminder comes at that time only on days when reviews are due then; once today's are done, today's goes. "
                + "Reminders are made on this phone from the due times it knows, and planned a week ahead. The widget and the "
                + "badge are updated when the app runs and, when iOS allows it, in the background.")
        }
        .task { await habits.refreshAuthorization() }
    }

    private func nextText(_ habits: HabitCenter) -> String {
        guard habits.reminderAuthorization == .allowed else { return "Not allowed" }
        guard let next = habits.scheduled.first else { return "None: no reviews due at that time this week" }
        return "\(next.title), \(next.fireDate.formatted(date: .abbreviated, time: .shortened))"
    }

    private func widgetText(_ habits: HabitCenter) -> String {
        if habits.widgetProblem != nil { return "Cannot get its data" }
        guard let snapshot = habits.snapshot else { return "Not updated yet" }
        guard let reviews = snapshot.reviews else { return "No review data yet" }
        let source = reviews.source == .server ? "from the server" : "from the questions on this phone"
        return "Updated \(reviews.asOf.formatted(date: .omitted, time: .shortened)), \(source)"
    }

    static func date(minutes: Int) -> Date {
        Calendar.current.date(bySettingHour: minutes / 60, minute: minutes % 60, second: 0, of: Date()) ?? Date()
    }

    static func minutes(of date: Date) -> Int {
        let parts = Calendar.current.dateComponents([.hour, .minute], from: date)
        return (parts.hour ?? 18) * 60 + (parts.minute ?? 0)
    }
}

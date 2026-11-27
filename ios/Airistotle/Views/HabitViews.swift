import SwiftUI

/// Today's goal as a ring with "6 of 10" in it.
struct GoalRing: View {
    let status: GoalStatus
    var size: CGFloat = 64

    var body: some View {
        ZStack {
            GoalRingGlyph(fraction: status.fraction, met: status.met, lineWidth: size / 10)
            VStack(spacing: 0) {
                Text("\(status.answersToday)")
                    .font(.system(size: size * 0.3, weight: .semibold, design: .rounded).monospacedDigit())
                    .foregroundStyle(Color.pageForeground)
                Text("of \(status.goal)")
                    .font(.system(size: size * 0.16).monospacedDigit())
                    .foregroundStyle(Color.pageMuted)
            }
        }
        .frame(width: size, height: size)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Today's goal: \(status.answersToday) of \(status.goal) answers")
    }
}

/// The ring, the streak and the best streak in one row (the review summary, the progress view).
struct GoalSummaryRow: View {
    let status: GoalStatus
    /// When the counts are older than the last refresh, say so.
    let staleNote: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 14) {
                GoalRing(status: status)
                VStack(alignment: .leading, spacing: 3) {
                    Text(status.met ? "Today's goal is met"
                         : "\(status.left) more \(status.left == 1 ? "answer" : "answers") for today's goal")
                        .font(.callout.weight(.semibold))
                        .foregroundStyle(Color.pageForeground)
                    Text(HabitText.streakLine(status))
                        .font(.footnote)
                        .foregroundStyle(Color.pageMuted)
                }
                Spacer(minLength: 0)
            }
            if let staleNote {
                Label(staleNote, systemImage: "clock.arrow.circlepath")
                    .font(.footnote)
                    .foregroundStyle(Color.pageWarn)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("goal.summary")
    }
}

enum HabitText {
    static func days(_ count: Int) -> String { count == 1 ? "1 day" : "\(count) days" }

    /// "Streak: 4 days · Best in 60 days: 9 days".
    static func streakLine(_ status: GoalStatus) -> String {
        let streak = status.streak > 0 ? "Streak: \(days(status.streak))" : "No streak yet"
        guard status.best > 0 else { return streak }
        return "\(streak) · Best in \(status.windowDays) days: \(days(status.best))"
    }

    /// For the review menu: "Progress: 6 of 10 today, 4-day streak".
    static func menuLine(_ status: GoalStatus?) -> String {
        guard let status else { return "Progress" }
        let streak = status.streak > 0 ? ", streak \(days(status.streak))" : ""
        return "Progress: \(status.answersToday) of \(status.goal) today\(streak)"
    }

    /// Honest about counts older than the last refresh (the server could not be asked).
    static func staleNote(_ status: GoalStatus, lastFetched: Date?, now: Date) -> String? {
        if lastFetched != nil, now.timeIntervalSince(status.asOf) < WidgetSnapshot.staleAfter { return nil }
        return "Counts from the server as of \(status.asOf.formatted(date: .abbreviated, time: .shortened)), with the answers "
            + "made on this phone since. Answers made elsewhere since then are not counted yet."
    }
}

/// The last weeks of answers, a week to a row, and today's goal.
struct StudyProgressView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    static let weeks = 8

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    if let status = model.habits.goalStatus, let habit = model.habits.snapshot?.habit {
                        GoalSummaryRow(status: status,
                                       staleNote: HabitText.staleNote(status, lastFetched: model.habits.lastFetched, now: Date()))
                        VStack(alignment: .leading, spacing: 10) {
                            Text("Last \(Self.weeks) weeks").font(.headline).foregroundStyle(Color.pageForeground)
                            AnswerCalendar(habit: habit, today: status.today, weeks: Self.weeks)
                            legend(goal: status.goal)
                        }
                        Text("A day counts towards the streak when its answers (on study pages and in reviews) reach your goal "
                            + "of \(status.goal). Answers made offline count as soon as you make them.")
                            .font(.footnote)
                            .foregroundStyle(Color.pageMuted)
                    } else {
                        Label("Your progress shows once Airistotle has read your answers from the server. Open the app while it "
                            + "can reach the server.", systemImage: "chart.bar")
                            .foregroundStyle(Color.pageMuted)
                            .accessibilityIdentifier("progress.none")
                    }
                }
                .padding(20)
            }
            .background(Color.pageBackground.ignoresSafeArea())
            .navigationTitle("Progress")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }.accessibilityIdentifier("progress.done")
                }
            }
        }
    }

    private func legend(goal: Int) -> some View {
        HStack(spacing: 14) {
            legendItem(AnswerCalendar.fill(answers: 0, goal: goal), "None")
            legendItem(AnswerCalendar.fill(answers: max(goal / 2, 1), goal: goal), "Some")
            legendItem(AnswerCalendar.fill(answers: goal, goal: goal), "Goal met")
        }
        .font(.caption)
        .foregroundStyle(Color.pageMuted)
        .accessibilityHidden(true)
    }

    private func legendItem(_ color: Color, _ label: String) -> some View {
        HStack(spacing: 5) {
            RoundedRectangle(cornerRadius: 3).fill(color).frame(width: 12, height: 12)
            Text(label)
        }
    }
}

/// Weeks as rows (Monday to Sunday), the current week last.
struct AnswerCalendar: View {
    let habit: WidgetSnapshot.Habit
    let today: LocalDay
    let weeks: Int

    var body: some View {
        let counts = habit.answersByDay
        let lastMonday = today.adding(days: 1 - today.isoWeekday)
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 4) {
                Text("").frame(width: 46)
                ForEach(["M", "T", "W", "T", "F", "S", "S"].indices, id: \.self) { index in
                    Text(["M", "T", "W", "T", "F", "S", "S"][index]).font(.caption2).foregroundStyle(Color.pageMuted)
                        .frame(maxWidth: .infinity)
                }
            }
            ForEach(0..<weeks, id: \.self) { row in
                let monday = lastMonday.adding(days: -7 * (weeks - 1 - row))
                HStack(spacing: 4) {
                    Text(Self.label(monday)).font(.caption2.monospacedDigit()).foregroundStyle(Color.pageMuted)
                        .frame(width: 46, alignment: .leading)
                    ForEach(0..<7, id: \.self) { column in
                        cell(monday.adding(days: column), counts: counts)
                    }
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("progress.calendar")
    }

    @ViewBuilder private func cell(_ day: LocalDay, counts: [LocalDay: Int]) -> some View {
        let known = day >= habit.firstDay && day <= today
        let answers = counts[day] ?? 0
        RoundedRectangle(cornerRadius: 4)
            .fill(known ? Self.fill(answers: answers, goal: habit.goal) : Color.clear)
            .overlay {
                RoundedRectangle(cornerRadius: 4)
                    .strokeBorder(day == today ? Color.pageForeground : (known ? Color.clear : Color.pageBorder),
                                  lineWidth: day == today ? 1.5 : 1)
            }
            .aspectRatio(1, contentMode: .fit)
            .frame(maxWidth: .infinity)
            .accessibilityLabel(known ? "\(day.description): \(answers) answers" : "\(day.description): not known")
    }

    static func fill(answers: Int, goal: Int) -> Color {
        if answers >= goal { return .pageOK }
        if answers > 0 { return Color.pageOK.opacity(0.25 + 0.5 * Double(answers) / Double(max(goal, 1))) }
        return .pageSurface2
    }

    static func label(_ monday: LocalDay) -> String {
        let date = monday.start(in: .current)
        return date.formatted(.dateTime.day().month(.abbreviated))
    }
}

/// "Today's goal is met": a light tap and a short note that does not stop anything.
struct GoalAcknowledgementModifier: ViewModifier {
    let habits: HabitCenter

    func body(content: Content) -> some View {
        content
            .overlay(alignment: .top) {
                if let note = habits.acknowledgement {
                    Label(note.text, systemImage: "checkmark.circle.fill")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Color.pageOK)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .background(Color.pageOKBackground, in: .capsule)
                        .padding(.top, 8)
                        .transition(.move(edge: .top).combined(with: .opacity))
                        .allowsHitTesting(false)
                        .accessibilityIdentifier("goal.acknowledgement")
                        .task(id: note.id) {
                            AccessibilityNotification.Announcement(note.text).post()
                            try? await Task.sleep(for: .seconds(3))
                            withAnimation { habits.dismissAcknowledgement() }
                        }
                }
            }
            .animation(.snappy, value: habits.acknowledgement)
            .sensoryFeedback(.impact(weight: .light), trigger: habits.acknowledgement) { _, new in new != nil }
    }
}

extension View {
    func goalAcknowledgement(_ habits: HabitCenter) -> some View {
        modifier(GoalAcknowledgementModifier(habits: habits))
    }
}

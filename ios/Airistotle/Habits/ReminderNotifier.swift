import Foundation
import UserNotifications

/// Whether the app may post reminders.
enum ReminderAuthorization: Equatable, Sendable {
    case notDetermined
    case denied
    case allowed
}

/// Local notifications and the app icon's badge (no push).
protocol ReminderNotifier: Sendable {
    func authorization() async -> ReminderAuthorization
    /// Ask the reader (the system's prompt, once); true when allowed.
    func requestAuthorization() async throws -> Bool
    /// Replace every scheduled reminder with `planned`.
    func replaceReminders(_ planned: [PlannedReminder], link: URL) async throws
    /// Remove every reminder, scheduled or delivered.
    func removeAll() async
    func setBadge(_ count: Int) async throws
}

/// `UNUserNotificationCenter`.
struct SystemReminderNotifier: ReminderNotifier {
    static let linkKey = "airistotleLink"

    private var center: UNUserNotificationCenter { .current() }

    func authorization() async -> ReminderAuthorization {
        switch await center.notificationSettings().authorizationStatus {
        case .notDetermined: .notDetermined
        case .denied: .denied
        default: .allowed
        }
    }

    func requestAuthorization() async throws -> Bool {
        try await center.requestAuthorization(options: [.alert, .sound, .badge])
    }

    func replaceReminders(_ planned: [PlannedReminder], link: URL) async throws {
        let pending = await center.pendingNotificationRequests().map(\.identifier)
            .filter { $0.hasPrefix(PlannedReminder.identifierPrefix) }
        let wanted = Set(planned.map(\.identifier))
        center.removePendingNotificationRequests(withIdentifiers: pending)
        // A delivered reminder of a day whose reviews are now done goes from the list too.
        let delivered = await center.deliveredNotifications().map(\.request.identifier)
            .filter { $0.hasPrefix(PlannedReminder.identifierPrefix) && !wanted.contains($0) }
        center.removeDeliveredNotifications(withIdentifiers: delivered)
        for reminder in planned {
            let content = UNMutableNotificationContent()
            content.title = reminder.title
            content.body = reminder.body
            content.sound = .default
            content.userInfo = [Self.linkKey: link.absoluteString]
            content.threadIdentifier = "reviews"
            if let badge = reminder.badge { content.badge = NSNumber(value: badge) }
            let trigger = UNTimeIntervalNotificationTrigger(timeInterval: max(reminder.fireDate.timeIntervalSinceNow, 1), repeats: false)
            try await center.add(UNNotificationRequest(identifier: reminder.identifier, content: content, trigger: trigger))
        }
    }

    func removeAll() async {
        center.removeAllPendingNotificationRequests()
        center.removeAllDeliveredNotifications()
    }

    func setBadge(_ count: Int) async throws {
        try await center.setBadgeCount(count)
    }
}

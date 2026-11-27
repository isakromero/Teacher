import AppIntents
import CoreSpotlight
import SwiftUI
import UserNotifications

@main
struct AiristotleApp: App {
    @State private var model: AppModel
    @Environment(\.scenePhase) private var scenePhase
    /// Taps on reminders (kept for the app's lifetime: the notification centre holds it weakly).
    private let notifications: NotificationRouter

    init() {
        let model: AppModel
        if ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil {
            // Under unit tests the app is only the tests' host: no Keychain, no network, and
            // nothing shared with the widget, the notification centre or Spotlight.
            let temporary = FileManager.default.temporaryDirectory
            model = AppModel(
                transport: UnreachableTransport(), store: EphemeralSessionStore(),
                cache: ResponseCache(root: temporary.appending(path: "AiristotleHostCache")),
                sharedContainer: .success(SharedContainer(root: temporary.appending(path: "AiristotleHostShared"))),
                notifier: InertReminderNotifier(), spotlight: InertSpotlightIndex()
            )
        } else {
            model = AppModel.live()
        }
        _model = State(initialValue: model)
        notifications = NotificationRouter(model: model)
        UNUserNotificationCenter.current().delegate = notifications
        // The Shortcuts (App Intents) reach the app through this.
        AppDependencyManager.shared.add(dependency: model)
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .task { await model.restore() }
                .onChange(of: scenePhase) { _, phase in
                    if phase == .active { model.becameActive() }
                    if phase == .background { model.wentToBackground() }
                }
                .onOpenURL { url in model.open(url: url) }
                .onContinueUserActivity(CSSearchableItemActionType) { activity in
                    if let identifier = activity.userInfo?[CSSearchableItemActivityIdentifier] as? String {
                        model.openSpotlightItem(identifier)
                    }
                }
                .onChange(of: model.phase) { _, phase in
                    // Only a study page on screen keeps the screen on (BrowserView); signed out, never.
                    if case .signedIn = phase { return }
                    UIApplication.shared.isIdleTimerDisabled = false
                }
        }
        // The widget's data, refreshed when iOS chooses to wake the app (Info.plist:
        // BGTaskSchedulerPermittedIdentifiers, UIBackgroundModes fetch).
        .backgroundTask(.appRefresh(HabitCenter.backgroundTaskIdentifier)) {
            await model.backgroundRefresh()
        }
    }
}

/// Reminders: a tap opens the review (through the same links as the widget); a reminder that
/// arrives while the app is open shows as a banner. The notification centre calls its delegate
/// on the main thread; the completion handlers are called there too (UIKit requires it).
@MainActor
final class NotificationRouter: NSObject, @preconcurrency UNUserNotificationCenterDelegate {
    private let model: AppModel

    init(model: AppModel) {
        self.model = model
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        defer { completionHandler() }
        // Not one of the app's reminders: nothing to open.
        guard let link = response.notification.request.content.userInfo[SystemReminderNotifier.linkKey] as? String else { return }
        guard let url = URL(string: link) else {
            model.linkProblem = "The reminder's link cannot be read (\(link))."
            return
        }
        model.open(url: url)
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }
}

/// Signed in or not.
struct RootView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        phaseView
            .alert("Link not opened", isPresented: Binding(get: { model.linkProblem != nil },
                                                           set: { if !$0 { model.linkProblem = nil } })) {
                Button("OK", role: .cancel) {}
                    .accessibilityIdentifier("link.problem.ok")
            } message: {
                Text(model.linkProblem ?? "")
            }
    }

    @ViewBuilder private var phaseView: some View {
        switch model.phase {
        case .launching:
            // The launch screen's colour, which is the page's.
            ProgressView()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color.pageBackground)
        case .signedOut(let notice):
            SignInView(notice: notice)
        case .signedIn(let session):
            BrowserView(session: session)
                .id(model.webSession) // a fresh web view for each session
        }
    }
}

/// The transport of the tests' host app: every request fails as unreachable.
struct UnreachableTransport: HTTPTransport {
    func send(_ request: URLRequest, followRedirects: Bool) async throws -> TransportResponse {
        throw URLError(.notConnectedToInternet)
    }
}

/// The tests' host app: no reminders.
struct InertReminderNotifier: ReminderNotifier {
    func authorization() async -> ReminderAuthorization { .denied }
    func requestAuthorization() async throws -> Bool { false }
    func replaceReminders(_ planned: [PlannedReminder], link: URL) async throws {}
    func removeAll() async {}
    func setBadge(_ count: Int) async throws {}
}

/// The tests' host app: no Spotlight.
struct InertSpotlightIndex: SpotlightIndexing {
    func index(_ entries: [SpotlightEntry]) async throws {}
    func remove(_ ids: [String]) async throws {}
    func removeAll() async throws {}
}

/// A session store in memory (the tests' host app).
final class EphemeralSessionStore: SessionStore, @unchecked Sendable {
    private let lock = NSLock()
    private var session: StoredSession?

    func load() throws -> StoredSession? { lock.withLock { session } }
    func save(_ session: StoredSession) throws { lock.withLock { self.session = session } }
    func delete() throws { lock.withLock { session = nil } }
}

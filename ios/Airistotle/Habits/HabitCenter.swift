import BackgroundTasks
import Foundation
import Observation
import os

/// What the phone itself holds that the summary does not: the saved review questions and the
/// answers made here.
struct LocalHabitData: Sendable {
    var queue: LocalReviewQueue?
    var pageAttempts: [Instant]
    var reviewsWaiting: [Instant]
    var reviewsAnswered: [LocalReviewAnswer]
}

/// "Today's goal is met", shown briefly once a day.
struct GoalAcknowledgement: Identifiable, Equatable {
    let id = UUID()
    var text: String
}

/// The phone summary and everything made from it: the widget's snapshot (written to the App
/// Group), the reminders, the badge, the daily goal and streak, and the "goal met" moment.
///
/// The summary comes from `GET /api/phone/summary` when the app becomes active, after a review
/// session, after a sync, after an answer on a page and in the background (when iOS allows it).
/// Offline, the review numbers come from the questions saved on the phone, and the goal counts
/// the answers waiting to upload as soon as they are made.
@MainActor
@Observable
final class HabitCenter {
    static let backgroundTaskIdentifier = "dev.airistotle.reader.refresh"
    private static let log = Logger(subsystem: "dev.airistotle.reader", category: "habits")

    /// The snapshot last made (what the widget shows).
    private(set) var snapshot: WidgetSnapshot?
    private(set) var summary: PhoneSummary?
    /// When the server's summary was last read in this session.
    private(set) var lastFetched: Date?
    /// Why the summary could not be read (the server refused or sent something unreadable).
    private(set) var fetchProblem: String?
    /// Why the widget cannot get its data (the App Group), or why it could not be written.
    private(set) var widgetProblem: String?
    /// Why the answers made on the phone could not all be counted.
    private(set) var localProblem: String?
    private(set) var reminderAuthorization: ReminderAuthorization = .notDetermined
    private(set) var reminderProblem: String?
    /// Why the background refresh could not be scheduled.
    private(set) var backgroundProblem: String?
    private(set) var acknowledgement: GoalAcknowledgement?
    /// The reminders scheduled last (for Settings and the tests).
    private(set) var scheduled: [PlannedReminder] = []

    @ObservationIgnored let preferences: HabitPreferences
    @ObservationIgnored let spotlight: SpotlightBook
    @ObservationIgnored private let gateway: Gateway
    @ObservationIgnored private let summaryRoot: URL
    @ObservationIgnored private let container: Result<SharedContainer, SharedContainer.Failure>
    @ObservationIgnored private let notifier: any ReminderNotifier
    @ObservationIgnored private let local: @Sendable (String) async throws -> LocalHabitData
    @ObservationIgnored private let reloadWidgets: @MainActor () -> Void
    @ObservationIgnored private let clock: @Sendable () -> Date
    @ObservationIgnored private let zone: @Sendable () -> TimeZone
    @ObservationIgnored var isOnline: @MainActor () -> Bool = { true }
    /// Says that today's goal is met in another way (Aristotle, `MascotCenter.goalMet`); true
    /// when it did, false (or nil) for the plain note (`acknowledgement`).
    @ObservationIgnored var announceGoal: (@MainActor (GoalStatus) -> Bool)?
    @ObservationIgnored private var summaryNamespace: String?
    @ObservationIgnored private var summaryIsCurrent = false
    @ObservationIgnored private var written: WidgetSnapshot?
    @ObservationIgnored private var lastGoal: GoalStatus?
    @ObservationIgnored private var pendingRefresh: Task<Void, Never>?
    @ObservationIgnored private var pendingFetch = false

    init(gateway: Gateway, summaryRoot: URL, container: Result<SharedContainer, SharedContainer.Failure>,
         notifier: any ReminderNotifier, spotlight: SpotlightBook, preferences: HabitPreferences,
         local: @escaping @Sendable (String) async throws -> LocalHabitData,
         reloadWidgets: @escaping @MainActor () -> Void,
         clock: @escaping @Sendable () -> Date = { Date() }, zone: @escaping @Sendable () -> TimeZone = { .current }) {
        self.gateway = gateway
        self.summaryRoot = summaryRoot
        self.container = container
        self.notifier = notifier
        self.spotlight = spotlight
        self.preferences = preferences
        self.local = local
        self.reloadWidgets = reloadWidgets
        self.clock = clock
        self.zone = zone
        if case .failure(let failure) = container {
            widgetProblem = failure.localizedDescription
            Self.log.error("App Group unavailable: \(failure.localizedDescription, privacy: .public)")
        }
    }

    // MARK: Settings

    var goal: Int {
        get {
            access(keyPath: \.goal)
            return preferences.goal
        }
        set {
            withMutation(keyPath: \.goal) { preferences.goal = newValue }
            Task { await rebuild() }
        }
    }

    var remindersOn: Bool {
        get {
            access(keyPath: \.remindersOn)
            return preferences.remindersOn
        }
        set { withMutation(keyPath: \.remindersOn) { preferences.remindersOn = newValue } }
    }

    var reminderMinutes: Int {
        get {
            access(keyPath: \.reminderMinutes)
            return preferences.reminderMinutes
        }
        set {
            withMutation(keyPath: \.reminderMinutes) { preferences.reminderMinutes = newValue }
            Task { await reschedule() }
        }
    }

    var badgeOn: Bool {
        get {
            access(keyPath: \.badgeOn)
            return preferences.badgeOn
        }
        set { withMutation(keyPath: \.badgeOn) { preferences.badgeOn = newValue } }
    }

    /// Turn the reminders on (asking for permission the first time) or off.
    func setReminders(_ enabled: Bool) async {
        if enabled {
            guard await ensureAuthorized() else {
                remindersOn = false
                return
            }
        }
        remindersOn = enabled
        await reschedule()
    }

    func setBadge(_ enabled: Bool) async {
        if enabled {
            guard await ensureAuthorized() else {
                badgeOn = false
                return
            }
        }
        badgeOn = enabled
        await reschedule()
    }

    private func ensureAuthorized() async -> Bool {
        reminderAuthorization = await notifier.authorization()
        switch reminderAuthorization {
        case .allowed: return true
        case .denied: return false
        case .notDetermined:
            do {
                let allowed = try await notifier.requestAuthorization()
                reminderAuthorization = allowed ? .allowed : .denied
                reminderProblem = nil
                return allowed
            } catch {
                reminderProblem = "Notifications could not be requested: \(error.localizedDescription)"
                return false
            }
        }
    }

    func refreshAuthorization() async {
        reminderAuthorization = await notifier.authorization()
    }

    // MARK: The goal

    /// Today's goal and the streak, now.
    var goalStatus: GoalStatus? {
        snapshot?.habit.map { HabitMath.status($0, at: clock()) }
    }

    /// The review numbers now.
    var reviewMoment: ReviewMoment? {
        guard let reviews = snapshot?.reviews else { return nil }
        return ReviewProjection.moment(reviews, habit: snapshot?.habit, at: clock())
    }

    /// The page "Continue studying" opens (from the last summary, read from the phone if it
    /// was not read in this session yet), if any.
    func currentResume() -> PhoneSummary.Resume? {
        if let namespace = gateway.connection?.cacheNamespace { loadCachedSummary(namespace) }
        return summary?.resume
    }

    func dismissAcknowledgement() { acknowledgement = nil }

    // MARK: Refreshing

    /// Read the summary from the server (when `fetch`), then make the snapshot again from it and
    /// what the phone holds, and pass it on.
    func refresh(fetch: Bool) async {
        guard let namespace = gateway.connection?.cacheNamespace else { return }
        loadCachedSummary(namespace)
        if fetch { await fetchSummary(namespace) }
        await rebuild()
    }

    /// A refresh a moment from now (several answers in a row make one request).
    /// A fetch asked for meanwhile is kept (a later local rebuild does not cancel it).
    func refreshSoon(fetch: Bool, delay: Duration = .seconds(3)) {
        pendingRefresh?.cancel()
        let fetch = fetch || pendingFetch
        pendingFetch = fetch
        pendingRefresh = Task { [weak self] in
            do { try await Task.sleep(for: delay) } catch { return } // cancelled: a newer refresh replaces it
            self?.pendingFetch = false
            await self?.refresh(fetch: fetch)
        }
    }

    private func summaryURL(_ namespace: String) -> URL {
        summaryRoot.appending(path: "\(namespace)/phone/summary.json")
    }

    private func loadCachedSummary(_ namespace: String) {
        guard summaryNamespace != namespace else { return }
        summaryNamespace = namespace
        summary = nil
        summaryIsCurrent = false
        let url = summaryURL(namespace)
        guard FileManager.default.fileExists(atPath: url.path(percentEncoded: false)) else { return }
        do {
            summary = try PhoneSummary(data: Data(contentsOf: url))
        } catch {
            fetchProblem = "The summary saved on this phone cannot be read (\(error.localizedDescription)); it is fetched again."
        }
    }

    private func fetchSummary(_ namespace: String) async {
        let query = StudyPageRequests.formEncode([("tz", zone().identifier)])
        let answer: (status: Int, headers: [String: String], body: Data)
        do {
            answer = try await gateway.fetch("GET", "/api/phone/summary?\(query)")
        } catch .unreachable, .notConnected {
            summaryIsCurrent = false
            return
        } catch {
            summaryIsCurrent = false
            fetchProblem = "The review summary could not be fetched: \(error.localizedDescription)"
            return
        }
        guard gateway.connection?.cacheNamespace == namespace else { return } // signed out meanwhile
        guard answer.status == 200 else {
            summaryIsCurrent = false
            if answer.status != 401 { // a refused session is the sign-in screen's to say
                fetchProblem = "The server refused the review summary (\(answer.status): "
                    + "\(AuthClient.detail(of: answer.body) ?? "no detail")). The widget shows what the phone has."
            }
            return
        }
        do {
            let fresh = try PhoneSummary(data: answer.body)
            summary = fresh
            summaryIsCurrent = true
            lastFetched = clock()
            fetchProblem = nil
            let url = summaryURL(namespace)
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try answer.body.write(to: url, options: .atomic)
        } catch let error as PhoneSummary.Invalid {
            summaryIsCurrent = false
            fetchProblem = error.message
        } catch {
            fetchProblem = "The review summary could not be kept on this phone: \(error.localizedDescription)"
        }
    }

    /// Make the snapshot again from what is known (no request), and pass it on.
    func rebuild() async {
        guard let namespace = gateway.connection?.cacheNamespace else { return }
        loadCachedSummary(namespace)
        let data: LocalHabitData
        do {
            data = try await local(namespace)
            localProblem = nil
        } catch {
            localProblem = "The answers made on this phone could not be counted (\(error.localizedDescription)), so today's "
                + "progress may be short."
            data = LocalHabitData(queue: nil, pageAttempts: [], reviewsWaiting: [], reviewsAnswered: [])
        }
        guard gateway.connection?.cacheNamespace == namespace else { return }
        let inputs = SnapshotBuilder.Inputs(
            now: clock(), goal: preferences.goal, summary: summary, summaryIsCurrent: summaryIsCurrent && isOnline(), queue: data.queue,
            localAnswers: SnapshotBuilder.localAnswers(pageAttempts: data.pageAttempts, reviewsWaiting: data.reviewsWaiting,
                                                       reviewsAnswered: data.reviewsAnswered, summaryTime: summary?.now),
            zone: zone())
        let made = SnapshotBuilder.snapshot(inputs)
        snapshot = made
        publish(made)
        await reschedule()
        acknowledgeGoal()
    }

    private func publish(_ made: WidgetSnapshot) {
        if let written, written.sameContent(as: made) { return }
        switch container {
        case .failure(let failure):
            widgetProblem = failure.localizedDescription
        case .success(let container):
            do {
                try container.writeSnapshot(made)
                written = made
                widgetProblem = nil
                reloadWidgets()
            } catch {
                widgetProblem = error.localizedDescription
                Self.log.error("Widget snapshot not written: \(error.localizedDescription, privacy: .public)")
            }
        }
    }

    /// Schedule the next week's reminders again, and set the badge.
    func reschedule() async {
        guard gateway.connection != nil, let reviews = snapshot?.reviews else { return }
        let now = clock()
        reminderAuthorization = await notifier.authorization()
        let allowed = reminderAuthorization == .allowed
        let planned = preferences.remindersOn && allowed
            ? ReminderPlanner.plan(.init(now: now, zone: zone(), minutes: preferences.reminderMinutes, reviews: reviews,
                                         habit: snapshot?.habit, badge: preferences.badgeOn))
            : []
        var sent = planned
        #if DEBUG
        if let testReminder, testReminder.fireDate > now { sent.append(testReminder) }
        #endif
        do {
            if allowed || !scheduled.isEmpty {
                try await notifier.replaceReminders(sent, link: DeepLink.review(quick: false).url)
            }
            scheduled = planned
            if allowed {
                let count = ReviewProjection.moment(reviews, habit: snapshot?.habit, at: now).state.dueCount
                try await notifier.setBadge(preferences.badgeOn ? count : 0)
            }
            reminderProblem = nil
        } catch {
            reminderProblem = "The reminders could not be scheduled: \(error.localizedDescription)"
        }
    }

    private func acknowledgeGoal() {
        guard let status = goalStatus else { return }
        defer { lastGoal = status }
        guard let last = lastGoal, last.today == status.today, !last.met, status.met,
              preferences.acknowledgedDay != status.today else { return }
        preferences.acknowledgedDay = status.today
        if announceGoal?(status) == true { return }
        acknowledgement = GoalAcknowledgement(text: "Today's goal is met: \(status.answersToday) answers.")
    }

    // MARK: Signing out

    /// Signed out: the widget says so, no reminder or badge stays, nothing stays searchable.
    func signedOut() async {
        pendingRefresh?.cancel()
        pendingFetch = false
        summary = nil
        summaryNamespace = nil
        summaryIsCurrent = false
        lastGoal = nil
        let made = WidgetSnapshot.signedOut(at: clock())
        snapshot = made
        publish(made)
        await notifier.removeAll()
        scheduled = []
        if await notifier.authorization() == .allowed {
            do { try await notifier.setBadge(0) } catch {
                reminderProblem = "The app icon's badge could not be cleared: \(error.localizedDescription)"
            }
        }
        await spotlight.removeAll()
    }

    #if DEBUG
    /// UI tests only (`-AiristotleReminderInSeconds <n>`): today's reminder, as planned, delivered
    /// `seconds` from now instead of at the chosen time.
    func scheduleTestReminder(in seconds: Double) async {
        guard let moment = reviewMoment, case .due(let count, let atLeast) = moment.state else {
            reminderProblem = "No review is due now, so there is no reminder to show."
            return
        }
        // Today's identifier: it takes the place of today's reminder at the chosen time.
        let reminder = PlannedReminder(day: LocalDay(clock(), in: zone()), fireDate: clock().addingTimeInterval(seconds), count: count,
                                       title: WidgetText.due(count, atLeast: atLeast),
                                       body: ReminderPlanner.body(count: count, goal: goalStatus),
                                       badge: preferences.badgeOn ? count : nil)
        testReminder = reminder
        await reschedule()
    }

    /// Kept through every reschedule until it is delivered.
    @ObservationIgnored private var testReminder: PlannedReminder?
    #endif

    // MARK: Background refresh

    /// Ask iOS to wake the app in an hour or more to refresh the summary. iOS decides whether
    /// and when that happens (it learns from how the app is used, and never in Low Power Mode or
    /// with Background App Refresh off).
    func scheduleBackgroundRefresh() {
        let request = BGAppRefreshTaskRequest(identifier: Self.backgroundTaskIdentifier)
        request.earliestBeginDate = clock().addingTimeInterval(3600)
        do {
            try BGTaskScheduler.shared.submit(request)
            backgroundProblem = nil
        } catch {
            backgroundProblem = "Background refresh could not be scheduled (\(error.localizedDescription)); the widget is "
                + "updated when the app runs."
            Self.log.error("BGAppRefreshTask not scheduled: \(error.localizedDescription, privacy: .public)")
        }
    }
}

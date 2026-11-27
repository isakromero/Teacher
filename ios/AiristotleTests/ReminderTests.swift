import XCTest
@testable import Airistotle

final class ReminderPlannerTests: XCTestCase {
    private let stockholm = TimeZone(identifier: "Europe/Stockholm") ?? .gmt

    /// 18:00 in Stockholm is 17:00 UTC in November.
    private func plan(_ made: WidgetSnapshot.Reviews, habit: WidgetSnapshot.Habit? = nil, now: String = "2026-11-26T15:00:00+00:00",
                      minutes: Int = 18 * 60, badge: Bool = false) -> [PlannedReminder] {
        ReminderPlanner.plan(.init(now: date(now), zone: stockholm, minutes: minutes, reviews: made, habit: habit, badge: badge))
    }

    func testOnlyDaysWithReviewsDueAtThatTimeGetOne() {
        // Due today; the next after that only on the 29th at 10:00 (the 29th's 18:00 sees it).
        let made = reviews(dueNow: 3, remaining: 20, upcoming: ["2026-11-29T09:00:00+00:00"])
        let planned = plan(made)
        // Today: 3; nothing new on the 27th or 28th, but those 3 are still due then (nobody answered them).
        XCTAssertEqual(planned.map(\.day), (0..<7).map { day("2026-11-26").adding(days: $0) })
        XCTAssertEqual(planned.first?.count, 3)
        XCTAssertEqual(planned.first?.fireDate, date("2026-11-26T17:00:00+00:00"))
        XCTAssertEqual(planned.first { $0.day == day("2026-11-29") }?.count, 4)
        XCTAssertEqual(planned.first?.title, "3 reviews due")
        XCTAssertEqual(planned.first?.identifier, "dev.airistotle.reminder.2026-11-26")
    }

    func testNothingDueMeansNoReminderAndTodaysGoesWhenAllAreDone() {
        // All done today; the next question comes due on the 28th at 08:00.
        let made = reviews(dueNow: 0, remaining: 12, done: 8, upcoming: ["2026-11-28T07:00:00+00:00"])
        let planned = plan(made)
        XCTAssertFalse(planned.contains { $0.day == day("2026-11-26") }, "today's reviews are done: no reminder today")
        XCTAssertFalse(planned.contains { $0.day == day("2026-11-27") })
        XCTAssertEqual(planned.first?.day, day("2026-11-28"))
        XCTAssertEqual(planned.first?.title, "1 review due")
        XCTAssertTrue(plan(reviews(dueNow: 0)).isEmpty)
    }

    func testATimeAlreadyPassedTodayIsSkipped() {
        let planned = plan(reviews(dueNow: 2), now: "2026-11-26T17:30:00+00:00")
        XCTAssertEqual(planned.first?.day, day("2026-11-27"))
        XCTAssertEqual(planned.count, 6)
    }

    func testCountsComingDueBeforeTheTimeAreIncludedAndTheCapHolds() {
        let made = reviews(dueNow: 1, remaining: 2, done: 18, upcoming: ["2026-11-26T16:00:00+00:00", "2026-11-26T16:30:00+00:00",
                                                                         "2026-11-26T18:00:00+00:00"])
        let planned = plan(made)
        XCTAssertEqual(planned.first?.count, 2, "today's cap leaves 2")
        XCTAssertEqual(planned.first { $0.day == day("2026-11-27") }?.count, 4, "tomorrow the cap starts again")
        XCTAssertEqual(plan(reviews(dueNow: 1, remaining: 0, done: 20)).first?.day, day("2026-11-27"))
    }

    func testTheStreakIsMentionedPlainlyOnlyWhileTodaysGoalIsNotMet() throws {
        let made = reviews(dueNow: 7)
        let going = habit(["2026-11-24": 10, "2026-11-25": 10, "2026-11-26": 6])
        let today = try XCTUnwrap(plan(made, habit: going).first)
        XCTAssertEqual(today.title, "7 reviews due")
        XCTAssertEqual(today.body, "7 review questions are ready. Your streak is 2 days; 4 more answers today meet your goal of 10.")
        let laterDay = try XCTUnwrap(plan(made, habit: going).last)
        XCTAssertFalse(laterDay.body.contains("streak"))
        let met = habit(["2026-11-25": 10, "2026-11-26": 12])
        XCTAssertEqual(plan(made, habit: met).first?.body, "7 review questions are ready. Tap to start the review.")
        let noStreak = habit(["2026-11-26": 3])
        XCTAssertFalse(plan(made, habit: noStreak).first?.body.contains("streak") ?? true)
    }

    func testTheBadgeGoesWithTheReminderOnlyWhenItIsOn() {
        XCTAssertNil(plan(reviews(dueNow: 3)).first?.badge)
        XCTAssertEqual(plan(reviews(dueNow: 3), badge: true).first?.badge, 3)
    }

    func testATimeThatDoesNotExistOnAClockChangeStillFires() throws {
        // Stockholm skips 02:00-03:00 on 2027-03-28.
        let fire = try XCTUnwrap(ReminderPlanner.fireDate(day("2027-03-28"), minutes: 2 * 60 + 30, zone: stockholm))
        XCTAssertEqual(LocalDay(fire, in: stockholm), day("2027-03-28"))
        let normal = try XCTUnwrap(ReminderPlanner.fireDate(day("2027-03-29"), minutes: 18 * 60, zone: stockholm))
        XCTAssertEqual(normal, date("2027-03-29T16:00:00+00:00")) // summer time: UTC+2
    }
}

/// Notifications recorded instead of shown.
final class FakeNotifier: ReminderNotifier, @unchecked Sendable {
    private let lock = NSLock()
    private struct State {
        var auth: ReminderAuthorization
        var grant: Bool
        var planned: [PlannedReminder] = []
        var badge: Int?
        var removedAll = 0
        var asked = 0
    }

    private var state: State

    init(authorization: ReminderAuthorization = .allowed, grant: Bool = true) {
        state = State(auth: authorization, grant: grant)
    }

    var planned: [PlannedReminder] { lock.withLock { state.planned } }
    var badge: Int? { lock.withLock { state.badge } }
    var removedAll: Int { lock.withLock { state.removedAll } }
    var asked: Int { lock.withLock { state.asked } }

    func authorization() async -> ReminderAuthorization { lock.withLock { state.auth } }

    func requestAuthorization() async throws -> Bool {
        lock.withLock {
            state.asked += 1
            state.auth = state.grant ? .allowed : .denied
            return state.grant
        }
    }

    func replaceReminders(_ planned: [PlannedReminder], link: URL) async throws {
        lock.withLock { state.planned = planned }
    }

    func removeAll() async {
        lock.withLock {
            state.planned = []
            state.removedAll += 1
        }
    }

    func setBadge(_ count: Int) async throws { lock.withLock { state.badge = count } }
}

final class FakeSpotlight: SpotlightIndexing, @unchecked Sendable {
    private let lock = NSLock()
    private var items: [String: SpotlightEntry] = [:]

    var entries: [String: SpotlightEntry] { lock.withLock { items } }

    func index(_ entries: [SpotlightEntry]) async throws {
        lock.withLock { for entry in entries { items[entry.id] = entry } }
    }

    func remove(_ ids: [String]) async throws {
        lock.withLock { for id in ids { items[id] = nil } }
    }

    func removeAll() async throws { lock.withLock { items = [:] } }
}

@MainActor
final class HabitCenterTests: XCTestCase {
    private struct Harness {
        let transport = FakeTransport()
        let gateway: Gateway
        let container: SharedContainer
        let notifier: FakeNotifier
        let spotlight = FakeSpotlight()
        let center: HabitCenter
        let defaults: UserDefaults
        let reloads: Counter

        @MainActor
        init(now: String = "2026-11-26T15:00:00+00:00", notifier: FakeNotifier = FakeNotifier(),
             container: Result<SharedContainer, SharedContainer.Failure>? = nil, local: LocalHabitData? = nil) {
            gateway = Gateway(transport: transport, cache: ResponseCache(root: TestFiles.folder()), events: { _ in })
            gateway.connection = .test
            let shared = SharedContainer(root: TestFiles.folder())
            self.container = shared
            self.notifier = notifier
            defaults = UserDefaults(suiteName: "HabitCenterTests-\(UUID().uuidString)") ?? .standard
            let reloads = Counter()
            self.reloads = reloads
            let moment = date(now)
            let data = local ?? LocalHabitData(queue: nil, pageAttempts: [], reviewsWaiting: [], reviewsAnswered: [])
            center = HabitCenter(gateway: gateway, summaryRoot: TestFiles.folder(), container: container ?? .success(shared),
                                 notifier: notifier, spotlight: SpotlightBook(index: spotlight, defaults: defaults),
                                 preferences: HabitPreferences(defaults: defaults), local: { _ in data },
                                 reloadWidgets: { reloads.count += 1 }, clock: { moment },
                                 zone: { TimeZone(identifier: "Europe/Stockholm") ?? .gmt })
        }

        func serve(_ body: Data, status: Int = 200) {
            transport.setHandler { request, _ in
                XCTAssertEqual(request.url?.path, "/api/phone/summary")
                XCTAssertEqual(request.url?.query, "tz=Europe%2FStockholm")
                return FakeTransport.answer(status, ["Content-Type": "application/json"], data: body)
            }
        }
    }

    final class Counter: @unchecked Sendable {
        var count = 0
    }

    private let summary = SummaryFixture.data(
        now: "2026-11-26T15:00:00+00:00", today: "2026-11-26", dueNow: 4, done: 3, remaining: 17,
        upcoming: ["2026-11-27T08:00:00+00:00"], days: [("2026-11-25", 8, 2), ("2026-11-26", 3, 3)],
        resume: ("waves", "Waves", "/artifacts/waves"))

    func testARefreshWritesTheWidgetsSnapshotAndSchedulesTheReminders() async throws {
        let harness = Harness()
        harness.serve(summary)
        await harness.center.setReminders(true)
        await harness.center.refresh(fetch: true)
        let written = try XCTUnwrap(try harness.container.readSnapshot())
        XCTAssertTrue(written.signedIn)
        XCTAssertEqual(written.reviews?.dueNow, 4)
        XCTAssertEqual(written.continueTitle, "Waves")
        XCTAssertEqual(harness.reloads.count, 1)
        XCTAssertEqual(harness.notifier.planned.first?.title, "4 reviews due")
        XCTAssertEqual(harness.notifier.planned.count, 7)
        XCTAssertEqual(harness.notifier.badge, 0, "the badge is off")
        XCTAssertEqual(harness.center.goalStatus?.answersToday, 6)
        XCTAssertEqual(harness.center.goalStatus?.streak, 1)
        XCTAssertEqual(harness.center.currentResume()?.target, "/artifacts/waves")
        // The same numbers again: the widget is not asked to reload.
        await harness.center.refresh(fetch: true)
        XCTAssertEqual(harness.reloads.count, 1)
    }

    func testTheBadgeShowsTheCountOnlyWhenTurnedOn() async throws {
        let harness = Harness()
        harness.serve(summary)
        await harness.center.refresh(fetch: true)
        XCTAssertTrue(harness.notifier.planned.isEmpty, "reminders are off by default")
        await harness.center.setBadge(true)
        XCTAssertEqual(harness.notifier.badge, 4)
        await harness.center.setBadge(false)
        XCTAssertEqual(harness.notifier.badge, 0)
    }

    func testDeniedPermissionLeavesRemindersOffAndSaysSo() async {
        let harness = Harness(notifier: FakeNotifier(authorization: .notDetermined, grant: false))
        harness.serve(summary)
        await harness.center.refresh(fetch: true)
        await harness.center.setReminders(true)
        XCTAssertEqual(harness.notifier.asked, 1)
        XCTAssertFalse(harness.center.remindersOn)
        XCTAssertEqual(harness.center.reminderAuthorization, .denied)
        XCTAssertTrue(harness.notifier.planned.isEmpty)
    }

    func testSigningOutClearsTheWidgetRemindersBadgeAndSpotlight() async throws {
        let harness = Harness()
        harness.serve(summary)
        await harness.center.setReminders(true)
        await harness.center.refresh(fetch: true)
        await harness.center.spotlight.update(namespace: "ns", pages: [])
        harness.gateway.connection = nil
        await harness.center.signedOut()
        XCTAssertEqual(try harness.container.readSnapshot()?.signedIn, false)
        XCTAssertEqual(harness.notifier.removedAll, 1)
        XCTAssertTrue(harness.notifier.planned.isEmpty)
        XCTAssertEqual(harness.notifier.badge, 0)
        XCTAssertNil(harness.center.goalStatus)
    }

    func testAnUnavailableAppGroupIsReportedAndNothingCrashes() async {
        let harness = Harness(container: .failure(.unavailable("no App Group in this build")))
        harness.serve(summary)
        XCTAssertNotNil(harness.center.widgetProblem)
        await harness.center.refresh(fetch: true)
        XCTAssertTrue(harness.center.widgetProblem?.contains("no App Group") == true)
        XCTAssertEqual(harness.reloads.count, 0)
        XCTAssertEqual(harness.center.snapshot?.reviews?.dueNow, 4, "the app still knows the numbers")
    }

    func testARefusedOrUnreadableSummaryIsShownAndTheSavedOneStays() async throws {
        let harness = Harness()
        harness.serve(summary)
        await harness.center.refresh(fetch: true)
        harness.serve(Data("{\"detail\":\"boom\"}".utf8), status: 500)
        await harness.center.refresh(fetch: true)
        XCTAssertTrue(harness.center.fetchProblem?.contains("500") == true)
        XCTAssertEqual(harness.center.snapshot?.reviews?.dueNow, 4)
        harness.serve(Data("{\"now\":1}".utf8))
        await harness.center.refresh(fetch: true)
        XCTAssertTrue(harness.center.fetchProblem?.contains("summary") == true)
    }

    func testTheGoalIsAcknowledgedOnceWhenItIsMet() async throws {
        let harness = Harness()
        harness.center.goal = 10
        harness.serve(summary) // 6 today
        await harness.center.refresh(fetch: true)
        XCTAssertNil(harness.center.acknowledgement)
        harness.serve(SummaryFixture.data(now: "2026-11-26T15:00:00+00:00", today: "2026-11-26", dueNow: 0,
                                          days: [("2026-11-26", 8, 3)]))
        await harness.center.refresh(fetch: true)
        XCTAssertEqual(harness.center.acknowledgement?.text, "Today's goal is met: 11 answers.")
        harness.center.dismissAcknowledgement()
        await harness.center.refresh(fetch: true)
        XCTAssertNil(harness.center.acknowledgement, "once a day")
    }

    func testAristotleCanSayTheGoalInsteadStillOnceADay() async throws {
        let harness = Harness()
        harness.center.goal = 10
        var said: [Int] = []
        var accept = true
        harness.center.announceGoal = { status in
            said.append(status.answersToday)
            return accept
        }
        harness.serve(summary) // 6 today
        await harness.center.refresh(fetch: true)
        harness.serve(SummaryFixture.data(now: "2026-11-26T15:00:00+00:00", today: "2026-11-26", dueNow: 0,
                                          days: [("2026-11-26", 8, 3)]))
        await harness.center.refresh(fetch: true)
        XCTAssertEqual(said, [11])
        XCTAssertNil(harness.center.acknowledgement, "Aristotle said it: no second note on top")
        await harness.center.refresh(fetch: true)
        XCTAssertEqual(said, [11], "once a day")

        // Aristotle turned off: the plain note, as before.
        let plain = Harness()
        plain.center.goal = 10
        accept = false
        plain.center.announceGoal = { _ in accept }
        plain.serve(summary)
        await plain.center.refresh(fetch: true)
        plain.serve(SummaryFixture.data(now: "2026-11-26T15:00:00+00:00", today: "2026-11-26", dueNow: 0,
                                        days: [("2026-11-26", 8, 3)]))
        await plain.center.refresh(fetch: true)
        XCTAssertEqual(plain.center.acknowledgement?.text, "Today's goal is met: 11 answers.")
    }

    func testOfflineTheCountComesFromTheSavedQuestions() async throws {
        let bundle = try ReviewFixture.bundle()
        let queue = LocalReviewQueue(bundle: bundle, answered: [])
        let harness = Harness(now: Instant.now().iso, local: LocalHabitData(queue: queue, pageAttempts: [], reviewsWaiting: [],
                                                                             reviewsAnswered: []))
        harness.transport.setHandler { _, _ in throw URLError(.notConnectedToInternet) }
        await harness.center.refresh(fetch: true)
        XCTAssertEqual(harness.center.snapshot?.reviews?.source, .phone)
        XCTAssertNil(harness.center.fetchProblem, "unreachable is not a problem to show: the phone's questions are used")
    }
}

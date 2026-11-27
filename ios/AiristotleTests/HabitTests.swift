import XCTest
@testable import Airistotle

/// A `GET /api/phone/summary` answer for the tests.
enum SummaryFixture {
    static func data(now: String, zone: String = "Europe/Stockholm", today: String, dueNow: Int = 0, cap: Int = 20, done: Int = 0,
                     remaining: Int = 20, upcoming: [String] = [], upcomingTotal: Int? = nil,
                     days: [(String, Int, Int)] = [], firstDay: String? = nil,
                     resume: (slug: String, title: String, url: String)? = nil) -> Data {
        let items = days.map { ["date": $0.0, "page_answers": $0.1, "review_answers": $0.2, "total": $0.1 + $0.2] as [String: Any] }
        var object: [String: Any] = [
            "now": now, "tz": zone, "today": today, "due_now": dueNow,
            "review": ["cap": cap, "done_today": done, "remaining_today": remaining, "items_total": 41,
                       "next_due_at": upcoming.first ?? NSNull()] as [String: Any],
            "upcoming_due": ["window_days": 7, "until": now, "total": upcomingTotal ?? upcoming.count, "limit": 100,
                             "times": upcoming] as [String: Any],
            "daily_answers": ["days": 60, "first_day": firstDay ?? days.first?.0 ?? today, "last_day": today, "items": items,
                              "streak_days": 0] as [String: Any],
            "continue": NSNull()
        ]
        if let resume {
            object["continue"] = ["artifact": ["slug": resume.slug, "title": resume.title], "url": resume.url,
                                  "last_studied_at": now, "active_seconds": 120.0] as [String: Any]
        }
        return (try? JSONSerialization.data(withJSONObject: object)) ?? Data()
    }

    static func summary(now: String, today: String, dueNow: Int = 0, cap: Int = 20, done: Int = 0, remaining: Int = 20,
                        upcoming: [String] = [], days: [(String, Int, Int)] = [], firstDay: String? = nil) throws -> PhoneSummary {
        try PhoneSummary(data: data(now: now, today: today, dueNow: dueNow, cap: cap, done: done, remaining: remaining,
                                    upcoming: upcoming, days: days, firstDay: firstDay))
    }
}

func date(_ iso: String) -> Date {
    Instant(isoString: iso)?.date ?? Date(timeIntervalSince1970: 0)
}

func day(_ text: String) -> LocalDay {
    LocalDay(text) ?? LocalDay(year: 1970, month: 1, day: 1)
}

final class LocalDayTests: XCTestCase {
    func testParsingIsStrict() {
        XCTAssertEqual(LocalDay("2026-11-26"), LocalDay(year: 2026, month: 11, day: 26))
        XCTAssertNil(LocalDay("2026-02-30"))
        XCTAssertNil(LocalDay("2026-1-5"))
        XCTAssertNil(LocalDay("2026-11-26T00:00"))
        XCTAssertEqual(LocalDay("2026-11-26")?.description, "2026-11-26")
    }

    func testArithmeticAcrossMonthsAndYears() {
        XCTAssertEqual(day("2026-12-31").adding(days: 1), day("2027-01-01"))
        XCTAssertEqual(day("2028-03-01").adding(days: -1), day("2028-02-29"))
        XCTAssertEqual(day("2026-11-01").days(to: day("2026-11-26")), 25)
        XCTAssertEqual(day("2026-11-26").isoWeekday, 4) // a Thursday
        XCTAssertEqual(day("2026-11-29").isoWeekday, 7)
    }

    func testAMomentFallsOnItsDayInTheZone() throws {
        let moment = date("2026-11-26T23:30:00+00:00")
        XCTAssertEqual(LocalDay(moment, in: try XCTUnwrap(TimeZone(identifier: "Europe/Stockholm"))), day("2026-11-27"))
        XCTAssertEqual(LocalDay(moment, in: try XCTUnwrap(TimeZone(identifier: "America/New_York"))), day("2026-11-26"))
    }

    func testStartOfDayOnAClockChange() throws {
        // São Paulo has no daylight saving now; Havana's clocks went forward at midnight (2025-03-09).
        let havana = try XCTUnwrap(TimeZone(identifier: "America/Havana"))
        let start = day("2025-03-09").start(in: havana)
        XCTAssertEqual(LocalDay(start, in: havana), day("2025-03-09"))
        XCTAssertEqual(LocalDay(start.addingTimeInterval(-1), in: havana), day("2025-03-08"))
    }
}

final class GoalStreakTests: XCTestCase {
    private func status(_ counts: [String: Int], today: String, goal: Int = 10, first: String = "2026-11-01") -> GoalStatus {
        HabitMath.status(answers: Dictionary(uniqueKeysWithValues: counts.map { (day($0.key), $0.value) }), firstDay: day(first),
                         today: day(today), goal: goal, asOf: Date())
    }

    func testStreakEndsTodayWhenTodaysGoalIsMet() {
        let result = status(["2026-11-24": 10, "2026-11-25": 12, "2026-11-26": 10], today: "2026-11-26")
        XCTAssertTrue(result.met)
        XCTAssertEqual(result.streak, 3)
    }

    func testStreakEndsYesterdayWhileTodaysGoalIsNotMet() {
        let result = status(["2026-11-24": 10, "2026-11-25": 12, "2026-11-26": 6], today: "2026-11-26")
        XCTAssertFalse(result.met)
        XCTAssertEqual(result.answersToday, 6)
        XCTAssertEqual(result.left, 4)
        XCTAssertEqual(result.streak, 2)
    }

    func testADayShortOfTheGoalEndsTheStreak() {
        // Any answer is not enough: the 24th had 9 of 10 (the server's own streak would count it).
        let result = status(["2026-11-22": 10, "2026-11-23": 10, "2026-11-24": 9, "2026-11-25": 10], today: "2026-11-26")
        XCTAssertEqual(result.streak, 1)
        XCTAssertEqual(result.best, 2)
    }

    func testNoStreakWhenYesterdayMissed() {
        XCTAssertEqual(status(["2026-11-24": 30, "2026-11-26": 3], today: "2026-11-26").streak, 0)
    }

    func testStreakStopsAtTheWindowsFirstDay() {
        let counts = Dictionary(uniqueKeysWithValues: (0..<10).map { (day("2026-11-17").adding(days: $0).description, 10) })
        let result = status(counts, today: "2026-11-26", first: "2026-11-20")
        XCTAssertEqual(result.streak, 7)
        XCTAssertEqual(result.windowDays, 7)
    }

    func testTheGoalChosenDecidesTheStreak() {
        let counts = ["2026-11-24": 6, "2026-11-25": 7, "2026-11-26": 5]
        XCTAssertEqual(status(counts, today: "2026-11-26", goal: 5).streak, 3)
        XCTAssertEqual(status(counts, today: "2026-11-26", goal: 10).streak, 0)
        XCTAssertEqual(status(counts, today: "2026-11-26", goal: 10).best, 0)
    }

    func testBestStreakInTheWindow() {
        let result = status(["2026-11-02": 10, "2026-11-03": 10, "2026-11-04": 10, "2026-11-05": 10, "2026-11-20": 10],
                            today: "2026-11-26")
        XCTAssertEqual(result.best, 4)
        XCTAssertEqual(result.streak, 0)
    }
}

final class PhoneSummaryTests: XCTestCase {
    func testDecodesTheServersSummary() throws {
        let data = SummaryFixture.data(now: "2026-11-26T15:00:00+00:00", today: "2026-11-26", dueNow: 3, done: 2, remaining: 18,
                                       upcoming: ["2026-11-26T17:00:00+00:00", "2026-11-26T16:00:00+00:00"],
                                       days: [("2026-11-25", 4, 1), ("2026-11-26", 2, 2)],
                                       resume: ("waves", "Waves", "/artifacts/waves"))
        let summary = try PhoneSummary(data: data)
        XCTAssertEqual(summary.dueNow, 3)
        XCTAssertEqual(summary.remainingToday, 18)
        XCTAssertEqual(summary.upcoming.map(\.date), [date("2026-11-26T16:00:00+00:00"), date("2026-11-26T17:00:00+00:00")])
        XCTAssertEqual(summary.days.last?.total, 4)
        XCTAssertEqual(summary.zone.identifier, "Europe/Stockholm")
        XCTAssertEqual(summary.resume, PhoneSummary.Resume(slug: "waves", title: "Waves", target: "/artifacts/waves"))
    }

    func testAMissingFieldIsAnErrorNotADefault() throws {
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: SummaryFixture.data(now: "2026-11-26T15:00:00+00:00",
                                                                                          today: "2026-11-26")) as? [String: Any])
        object["due_now"] = nil
        XCTAssertThrowsError(try PhoneSummary(data: JSONSerialization.data(withJSONObject: object))) { error in
            XCTAssertTrue((error as? PhoneSummary.Invalid)?.message.contains("due_now") == true)
        }
        object["due_now"] = 1
        object["continue"] = nil
        XCTAssertThrowsError(try PhoneSummary(data: JSONSerialization.data(withJSONObject: object)))
        object["continue"] = NSNull()
        object["tz"] = "Mars/Olympus"
        XCTAssertThrowsError(try PhoneSummary(data: JSONSerialization.data(withJSONObject: object)))
        XCTAssertThrowsError(try PhoneSummary(data: Data("not json".utf8)))
    }

    func testContinueOpensOnlyTheServersOwnPages() {
        XCTAssertEqual(PhoneSummary.resumeTarget("/artifacts/waves"), "/artifacts/waves")
        XCTAssertEqual(PhoneSummary.resumeTarget("/labs/pendulum-2"), "/labs/pendulum-2")
        XCTAssertNil(PhoneSummary.resumeTarget("https://example.com/artifacts/waves"))
        XCTAssertNil(PhoneSummary.resumeTarget("//evil.example/x"))
        XCTAssertNil(PhoneSummary.resumeTarget("/artifacts/../admin"))
        XCTAssertNil(PhoneSummary.resumeTarget("/artifacts/waves?x=1"))
        XCTAssertNil(PhoneSummary.resumeTarget("/settings"))
    }
}

final class SnapshotBuilderTests: XCTestCase {
    private let stockholm = TimeZone(identifier: "Europe/Stockholm") ?? .gmt

    private func inputs(summary: PhoneSummary?, current: Bool, queue: LocalReviewQueue? = nil, local: [Date] = [],
                        now: Date) -> SnapshotBuilder.Inputs {
        SnapshotBuilder.Inputs(now: now, goal: 10, summary: summary, summaryIsCurrent: current, queue: queue, localAnswers: local,
                               zone: stockholm)
    }

    func testTheServersNumbersWhenItWasJustAsked() throws {
        let summary = try SummaryFixture.summary(now: "2026-11-26T15:00:00+00:00", today: "2026-11-26", dueNow: 3, done: 2,
                                                 remaining: 18, upcoming: ["2026-11-26T16:00:00+00:00"],
                                                 days: [("2026-11-26", 4, 2)])
        let snapshot = SnapshotBuilder.snapshot(inputs(summary: summary, current: true, now: date("2026-11-26T15:00:05+00:00")))
        let reviews = try XCTUnwrap(snapshot.reviews)
        XCTAssertEqual(reviews.source, .server)
        XCTAssertEqual(reviews.dueNow, 3)
        XCTAssertEqual(reviews.doneToday, 2)
        XCTAssertEqual(reviews.computedAt, date("2026-11-26T15:00:00+00:00"))
        XCTAssertEqual(reviews.upcoming, [date("2026-11-26T16:00:00+00:00")])
        XCTAssertTrue(reviews.upcomingComplete)
        XCTAssertEqual(snapshot.habit?.days, [WidgetSnapshot.DayCount(day: day("2026-11-26"), answers: 6)])
        XCTAssertTrue(snapshot.signedIn)
    }

    func testOfflineTheSavedQuestionsGiveTheCount() async throws {
        let bundle = try ReviewFixture.bundle()
        let queue = LocalReviewQueue(bundle: bundle, answered: [])
        let now = bundle.bundledAt.adding(days: 0.01)
        let summary = try SummaryFixture.summary(now: "2020-01-01T00:00:00+00:00", today: "2020-01-01", dueNow: 99)
        let snapshot = SnapshotBuilder.snapshot(inputs(summary: summary, current: false, queue: queue, now: now.date))
        let reviews = try XCTUnwrap(snapshot.reviews)
        XCTAssertEqual(reviews.source, .phone)
        XCTAssertEqual(reviews.dueNow, queue.due(now: now).count)
        XCTAssertEqual(reviews.remainingToday, queue.remaining(now: now))
        XCTAssertEqual(reviews.asOf, bundle.bundledAt.date)
        XCTAssertEqual(reviews.upcoming, queue.upcoming(after: now).map(\.date))
    }

    func testWithoutSavedQuestionsTheLastSummaryIsUsed() throws {
        let summary = try SummaryFixture.summary(now: "2026-11-26T15:00:00+00:00", today: "2026-11-26", dueNow: 2)
        let reviews = try XCTUnwrap(SnapshotBuilder.reviews(inputs(summary: summary, current: false,
                                                                    now: date("2026-11-26T19:00:00+00:00"))))
        XCTAssertEqual(reviews.source, .server)
        XCTAssertEqual(reviews.asOf, date("2026-11-26T15:00:00+00:00"))
        XCTAssertNil(SnapshotBuilder.reviews(inputs(summary: nil, current: false, now: Date())))
    }

    func testAnswersMadeOfflineCountAtOnce() throws {
        let summary = try SummaryFixture.summary(now: "2026-11-26T12:00:00+00:00", today: "2026-11-26",
                                                 days: [("2026-11-25", 10, 0), ("2026-11-26", 5, 3)])
        // Two answers waiting to upload, made this afternoon (Stockholm).
        let local = [date("2026-11-26T13:00:00+00:00"), date("2026-11-26T13:05:00+00:00")]
        let habit = try XCTUnwrap(SnapshotBuilder.habit(inputs(summary: summary, current: false, local: local, now: Date())))
        let status = HabitMath.status(habit, at: date("2026-11-26T14:00:00+00:00"))
        XCTAssertEqual(status.answersToday, 10)
        XCTAssertTrue(status.met)
        XCTAssertEqual(status.streak, 2)
    }

    func testAnOfflineAnswerCountsOnTheDayItWasMadeInTheZone() throws {
        let summary = try SummaryFixture.summary(now: "2026-11-26T12:00:00+00:00", today: "2026-11-26",
                                                 days: [("2026-11-26", 9, 0)])
        // 22:59 UTC is 23:59 in Stockholm (the 26th); 23:01 UTC is 00:01 there (the 27th).
        let local = [date("2026-11-26T22:59:00+00:00"), date("2026-11-26T23:01:00+00:00")]
        let habit = try XCTUnwrap(SnapshotBuilder.habit(inputs(summary: summary, current: false, local: local, now: Date())))
        XCTAssertEqual(habit.answersByDay[day("2026-11-26")], 10)
        XCTAssertEqual(habit.answersByDay[day("2026-11-27")], 1)
        let nextDay = HabitMath.status(habit, at: date("2026-11-27T08:00:00+00:00"))
        XCTAssertEqual(nextDay.answersToday, 1)
        XCTAssertEqual(nextDay.streak, 1) // the 26th met the goal; the 27th not yet
    }

    func testWhichPhoneAnswersTheSummaryDoesNotCount() {
        let summaryTime = Instant(isoString: "2026-11-26T12:00:00+00:00")
        func answer(_ time: String, pending: Bool) -> LocalReviewAnswer {
            LocalReviewAnswer(itemID: 1, presentation: "p", slug: "s", madeAt: Instant(isoString: time) ?? Instant(micros: 0),
                              verdict: "passed", intervalDays: 1, dueAt: Instant(micros: 0), pending: pending)
        }
        let counted = SnapshotBuilder.localAnswers(
            pageAttempts: [Instant(isoString: "2026-11-25T09:00:00+00:00")].compactMap(\.self),
            reviewsWaiting: [Instant(isoString: "2026-11-26T10:00:00+00:00")].compactMap(\.self),
            reviewsAnswered: [answer("2026-11-26T11:00:00+00:00", pending: false), // the server counted it
                              answer("2026-11-26T13:00:00+00:00", pending: false), // answered online since
                              answer("2026-11-26T10:00:00+00:00", pending: true)], // the waiting one, counted once
            summaryTime: summaryTime)
        XCTAssertEqual(counted, [date("2026-11-25T09:00:00+00:00"), date("2026-11-26T10:00:00+00:00"),
                                 date("2026-11-26T13:00:00+00:00")])
    }
}

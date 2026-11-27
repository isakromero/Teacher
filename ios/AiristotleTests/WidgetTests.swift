import XCTest
@testable import Airistotle

/// Review numbers for the tests (Stockholm; computed at 15:00 UTC = 16:00 there).
func reviews(dueNow: Int = 0, cap: Int = 20, remaining: Int = 20, done: Int = 0, upcoming: [String] = [], complete: Bool = true,
             computedAt: String = "2026-11-26T15:00:00+00:00", asOf: String? = nil) -> WidgetSnapshot.Reviews {
    WidgetSnapshot.Reviews(computedAt: date(computedAt), asOf: date(asOf ?? computedAt), source: .server, timeZone: "Europe/Stockholm",
                           dueNow: dueNow, cap: cap, remainingToday: remaining, doneToday: done, upcoming: upcoming.map(date),
                           upcomingComplete: complete)
}

func habit(_ counts: [String: Int], goal: Int = 10, first: String = "2026-10-01", asOf: String = "2026-11-26T15:00:00+00:00")
    -> WidgetSnapshot.Habit {
    WidgetSnapshot.Habit(goal: goal, timeZone: "Europe/Stockholm", firstDay: day(first),
                         days: counts.map { WidgetSnapshot.DayCount(day: day($0.key), answers: $0.value) }.sorted { $0.day < $1.day },
                         asOf: date(asOf))
}

final class WidgetSnapshotTests: XCTestCase {
    private func sample() -> WidgetSnapshot {
        WidgetSnapshot(writtenAt: date("2026-11-26T15:00:01.250+00:00"), signedIn: true,
                       reviews: reviews(dueNow: 3, upcoming: ["2026-11-26T16:30:00.5+00:00"]),
                       habit: habit(["2026-11-26": 4]), continueTitle: "Waves")
    }

    func testEncodesAndDecodesExactly() throws {
        let snapshot = sample()
        let decoded = try WidgetSnapshot.decode(snapshot.encoded())
        XCTAssertEqual(decoded, snapshot)
        let text = try snapshot.encoded().text
        XCTAssertTrue(text.contains("\"version\":1"))
        XCTAssertFalse(text.lowercased().contains("token"), "the snapshot never holds the session")
    }

    func testAnotherVersionOrDamagedFileIsRefusedWithAReason() throws {
        var other = try XCTUnwrap(JSONSerialization.jsonObject(with: sample().encoded()) as? [String: Any])
        other["version"] = 2
        XCTAssertThrowsError(try WidgetSnapshot.decode(JSONSerialization.data(withJSONObject: other))) { error in
            XCTAssertTrue((error as? WidgetSnapshot.Invalid)?.message.contains("another version") == true)
        }
        XCTAssertThrowsError(try WidgetSnapshot.decode(Data("{\"version\":1}".utf8)))
        XCTAssertThrowsError(try WidgetSnapshot.decode(Data("garbage".utf8)))
    }

    func testSameContentIgnoresWhenItWasWritten() {
        var later = sample()
        later.writtenAt = later.writtenAt.addingTimeInterval(60)
        XCTAssertTrue(later.sameContent(as: sample()))
        later.reviews?.dueNow = 4
        XCTAssertFalse(later.sameContent(as: sample()))
    }

    func testSharedContainerWritesReadsAndSaysWhenItCannot() throws {
        let container = SharedContainer(root: TestFiles.folder())
        XCTAssertEqual(try container.readSnapshot(), nil)
        try container.writeSnapshot(sample())
        XCTAssertEqual(try container.readSnapshot(), sample())
        try Data("damaged".utf8).write(to: container.snapshotURL)
        XCTAssertThrowsError(try container.readSnapshot()) { error in
            guard case .unreadable = error as? SharedContainer.Failure else { return XCTFail("\(error)") }
        }
        try container.removeSnapshot()
        XCTAssertEqual(try container.readSnapshot(), nil)
    }

    func testAnUnavailableGroupIsShownNotHidden() {
        let display = ReviewProjection.display(.failure(.unavailable("no entitlement")), at: Date())
        guard case .unavailable(let reason) = display else { return XCTFail("\(display)") }
        XCTAssertTrue(reason.contains("no entitlement"))
        XCTAssertEqual(ReviewProjection.timeline(.failure(.unavailable("x")), now: Date()).entries.count, 1)
    }

    func testStalenessAndItsWording() {
        let fresh = ReviewProjection.moment(reviews(dueNow: 2), habit: nil, at: date("2026-11-26T17:59:00+00:00"))
        XCTAssertFalse(fresh.stale)
        XCTAssertNil(WidgetText.asOf(fresh))
        let old = ReviewProjection.moment(reviews(dueNow: 2), habit: nil, at: date("2026-11-26T18:01:00+00:00"))
        XCTAssertTrue(old.stale)
        var time = Date.FormatStyle(date: .omitted, time: .shortened)
        time.timeZone = TimeZone(identifier: "Europe/Stockholm") ?? .gmt
        XCTAssertEqual(WidgetText.asOf(old), "Updated \(date("2026-11-26T15:00:00+00:00").formatted(time))")
        let nextDay = ReviewProjection.moment(reviews(dueNow: 2), habit: nil, at: date("2026-11-27T09:00:00+00:00"))
        XCTAssertEqual(WidgetText.asOf(nextDay), "Updated yesterday \(date("2026-11-26T15:00:00+00:00").formatted(time))")
    }
}

final class ReviewTimelineTests: XCTestCase {
    private func snapshot(_ reviews: WidgetSnapshot.Reviews, habit: WidgetSnapshot.Habit? = nil) -> WidgetSnapshot {
        WidgetSnapshot(writtenAt: reviews.computedAt, signedIn: true, reviews: reviews, habit: habit, continueTitle: nil)
    }

    func testTheCountRisesAtEachDueTimeWithoutTheApp() {
        let made = reviews(dueNow: 1, upcoming: ["2026-11-26T15:30:00+00:00", "2026-11-26T15:30:00+00:00", "2026-11-26T16:10:00+00:00"])
        let (entries, reload) = ReviewProjection.timeline(.success(snapshot(made)), now: date("2026-11-26T15:00:00+00:00"))
        let counts = entries.compactMap { entry -> (Date, Int)? in
            guard case .reviews(let moment, _) = entry.1 else { return nil }
            return (entry.0, moment.state.dueCount)
        }
        XCTAssertEqual(counts.first?.1, 1)
        XCTAssertEqual(counts.first { $0.0 == date("2026-11-26T15:30:00+00:00") }?.1, 3)
        XCTAssertEqual(counts.first { $0.0 == date("2026-11-26T16:10:00+00:00") }?.1, 4)
        XCTAssertNotNil(reload)
    }

    func testTheCapHoldsTodayAndStartsAgainAtMidnight() {
        // 20 a day; 18 done; 5 due: 2 may be answered today.
        let made = reviews(dueNow: 5, remaining: 2, done: 18)
        XCTAssertEqual(ReviewProjection.moment(made, habit: nil, at: date("2026-11-26T16:00:00+00:00")).state, .due(2, atLeast: false))
        let capped = reviews(dueNow: 5, remaining: 0, done: 20)
        let tonight = ReviewProjection.moment(capped, habit: nil, at: date("2026-11-26T20:00:00+00:00"))
        // Stockholm's midnight is 23:00 UTC.
        XCTAssertEqual(tonight.state, .doneToday(next: date("2026-11-26T23:00:00+00:00")))
        XCTAssertEqual(WidgetText.headline(tonight).detail, "More due tomorrow")
        let tomorrow = ReviewProjection.moment(capped, habit: nil, at: date("2026-11-26T23:00:00+00:00"))
        XCTAssertEqual(tomorrow.state, .due(5, atLeast: false))
        let dates = ReviewProjection.changeDates(snapshot(capped), from: date("2026-11-26T15:00:00+00:00"))
        XCTAssertTrue(dates.contains(date("2026-11-26T23:00:00+00:00")))
    }

    func testDoneTodayIsNotTheSameAsNothingDue() {
        let next = "2026-11-27T08:00:00+00:00"
        let done = ReviewProjection.moment(reviews(dueNow: 0, remaining: 15, done: 5, upcoming: [next]), habit: nil,
                                           at: date("2026-11-26T16:00:00+00:00"))
        XCTAssertEqual(done.state, .doneToday(next: date(next)))
        XCTAssertEqual(WidgetText.headline(done).title, "All done today")
        let idle = ReviewProjection.moment(reviews(dueNow: 0, upcoming: [next]), habit: nil, at: date("2026-11-26T16:00:00+00:00"))
        XCTAssertEqual(idle.state, .nothingDue(next: date(next)))
        XCTAssertEqual(WidgetText.headline(idle).title, "Nothing due")
        XCTAssertTrue(WidgetText.headline(idle).detail?.hasPrefix("until tomorrow") == true)
        let none = ReviewProjection.moment(reviews(dueNow: 0), habit: nil, at: date("2026-11-26T16:00:00+00:00"))
        XCTAssertEqual(WidgetText.headline(none).detail, "No reviews scheduled")
        // The goal met with nothing due is a day done, too.
        let met = ReviewProjection.moment(reviews(dueNow: 0), habit: habit(["2026-11-26": 10]), at: date("2026-11-26T16:00:00+00:00"))
        XCTAssertEqual(met.state, .doneToday(next: nil))
    }

    func testDoneTodayBecomesDueWhenTheNextComesDue() {
        let made = reviews(dueNow: 0, remaining: 15, done: 5, upcoming: ["2026-11-26T18:00:00+00:00"])
        let (entries, _) = ReviewProjection.timeline(.success(snapshot(made)), now: date("2026-11-26T15:00:00+00:00"))
        let states = entries.compactMap { entry -> ReviewState? in
            if case .reviews(let moment, _) = entry.1 { moment.state } else { nil }
        }
        XCTAssertEqual(states.first, .doneToday(next: date("2026-11-26T18:00:00+00:00")))
        XCTAssertTrue(states.contains(.due(1, atLeast: false)))
    }

    func testTheGoalStartsAgainAtMidnightAndTheStreakCarries() {
        let made = reviews(dueNow: 0)
        let counts = habit(["2026-11-25": 10, "2026-11-26": 10])
        let evening = ReviewProjection.moment(made, habit: counts, at: date("2026-11-26T20:00:00+00:00"))
        XCTAssertEqual(evening.goal?.answersToday, 10)
        XCTAssertEqual(evening.goal?.streak, 2)
        let morning = ReviewProjection.moment(made, habit: counts, at: date("2026-11-27T07:00:00+00:00"))
        XCTAssertEqual(morning.goal?.answersToday, 0)
        XCTAssertEqual(morning.goal?.streak, 2) // ends yesterday until today's is met
    }

    func testAStaleEntryIsAddedWhenTheNumbersAge() {
        let made = reviews(dueNow: 2)
        let (entries, _) = ReviewProjection.timeline(.success(snapshot(made)), now: date("2026-11-26T15:00:00+00:00"))
        let staleEntry = entries.first { entry in
            if case .reviews(let moment, _) = entry.1 { moment.stale } else { false }
        }
        XCTAssertEqual(staleEntry?.0, date("2026-11-26T18:00:01+00:00"))
    }

    func testCountsPastATruncatedListAreAtLeast() {
        let made = reviews(dueNow: 1, upcoming: ["2026-11-26T15:10:00+00:00"], complete: false)
        let before = ReviewProjection.moment(made, habit: nil, at: date("2026-11-26T15:05:00+00:00"))
        XCTAssertEqual(before.state, .due(1, atLeast: false))
        let after = ReviewProjection.moment(made, habit: nil, at: date("2026-11-26T15:20:00+00:00"))
        XCTAssertEqual(after.state, .due(2, atLeast: true))
        XCTAssertEqual(WidgetText.headline(after).title, "2+ reviews due")
    }

    func testSignedOutAndNoDataSaySoAndShowNoNumber() {
        let out = ReviewProjection.timeline(.success(.signedOut(at: Date())), now: Date())
        XCTAssertEqual(out.entries.map(\.1), [.signedOut])
        XCTAssertNil(out.reloadAt)
        XCTAssertEqual(ReviewProjection.display(.success(nil), at: Date()), .noData)
        let empty = WidgetSnapshot(writtenAt: Date(), signedIn: true, reviews: nil, habit: nil, continueTitle: nil)
        XCTAssertEqual(ReviewProjection.display(.success(empty), at: Date()), .noData)
        XCTAssertNil(ReviewWidgetView.link(.signedOut))
        XCTAssertEqual(ReviewWidgetView.link(ReviewProjection.display(.success(snapshot(reviews(dueNow: 1))), at: Date())),
                       URL(string: "airistotle://review"))
    }

    func testTheTimelineIsBoundedAndMergesEqualMoments() {
        let many = (1...300).map { Instant(date("2026-11-26T15:00:00+00:00").addingTimeInterval(Double($0) * 60)).iso }
        let made = reviews(dueNow: 0, remaining: 3, upcoming: many)
        let (entries, reload) = ReviewProjection.timeline(.success(snapshot(made)), now: date("2026-11-26T15:00:00+00:00"))
        XCTAssertLessThanOrEqual(entries.count, ReviewProjection.maxEntries)
        // After 3 due, the cap holds the count: those minutes are merged into one entry.
        XCTAssertLessThan(entries.count, 10)
        XCTAssertNotNil(reload)
    }
}

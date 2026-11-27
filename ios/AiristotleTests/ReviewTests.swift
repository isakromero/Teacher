import XCTest
@testable import Airistotle

/// A moment the tests move by hand.
final class TestClock: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Instant

    init(_ value: Instant) { self.value = value }

    var now: Instant {
        get { lock.withLock { value } }
        set { lock.withLock { value = newValue } }
    }

    func advance(days: Double) { now = now.adding(days: days) }
}

/// A review service on the offline harness's gateway, with the saved bundle of the fixture.
struct ReviewHarness {
    let base: OfflineHarness
    let store: ReviewStore
    let service: ReviewService
    let clock: TestClock

    init(judge: any BlurbJudge = FakeJudge(reply: nil), now: Instant? = nil) throws {
        base = OfflineHarness(judge: judge)
        let bundle = try ReviewFixture.bundle()
        let clock = TestClock(now ?? bundle.bundledAt.adding(days: 0.01))
        self.clock = clock
        store = ReviewStore(root: base.store.root)
        service = ReviewService(gateway: base.gateway, store: store, pages: base.store, grader: BlurbGrader(judge: judge),
                                clock: { clock.now }, timeZone: { TimeZone(identifier: "Europe/Stockholm") ?? .gmt }, changed: {})
    }

    var namespace: String { base.namespace }

    func saveFixtureBundle() async throws {
        try await store.saveBundle(namespace, try ReviewFixture.bundle())
    }

    func item(_ type: String) async throws -> ReviewItem {
        let queue = try await service.localQueue()
        return try XCTUnwrap(queue?.askable(now: clock.now).first { $0.type == type })
    }
}

// swiftlint:disable large_tuple
extension ReviewFixture {
    /// A bundle of synthetic items made from the fixture's first one.
    static func bundle(items: [(id: Int, slug: String, due: Instant, dueNow: Bool, confident: Bool)]) throws -> ReviewBundle {
        let base = try bundle()
        let template = base.items[0].raw.object ?? [:]
        let made = items.map { spec -> JSONValue in
            var fields = template
            fields["id"] = .int(spec.id)
            fields["presentation"] = .string("token-\(spec.id)")
            fields["artifact"] = .object(["slug": .string(spec.slug), "title": .string(spec.slug.uppercased())])
            fields["due_at"] = .string(spec.due.iso)
            fields["due_now"] = .bool(spec.dueNow)
            fields["confident_error"] = .bool(spec.confident)
            return .object(fields)
        }
        guard case .object(var raw) = base.raw else { throw XCTSkip("no bundle") }
        raw["items"] = .array(made)
        return try ReviewBundle(.object(raw))
    }
}
// swiftlint:enable large_tuple

final class ReviewServiceTests: XCTestCase {
    func testAnOfflineAnswerIsGradedQueuedAndLeavesTheSavedQueue() async throws {
        let harness = try ReviewHarness()
        try await harness.saveFixtureBundle()
        harness.base.offline()
        let item = try await harness.item("multiple_choice")
        let correct = try XCTUnwrap(item.answerKey["correct_index"].int)
        let wrong = (correct + 1) % item.options.count
        let result = try await harness.service.answerOffline(item, response: .object(["choice": .int(wrong)]), confidence: .high)
        XCTAssertEqual(result.verdict, "failed")
        XCTAssertTrue(result.gradedOnPhone)
        XCTAssertEqual(result.intervalDays, 1.0, "a failed review starts again at the first interval")
        XCTAssertEqual(result.dueAt, harness.clock.now.adding(days: 1))
        XCTAssertEqual(result.revealed, item.answerKey)

        // On disk before the result was shown, in the upload's shape.
        let pending = try await harness.store.pending(harness.namespace)
        XCTAssertEqual(pending.count, 1)
        XCTAssertEqual(pending[0].upload["item_id"], .int(item.id))
        XCTAssertEqual(pending[0].upload["presentation"], .string(item.presentation))
        XCTAssertEqual(pending[0].upload["grader"], .string("device:airistotle-rules"))
        XCTAssertEqual(pending[0].upload["confidence"], .string("high"))
        XCTAssertEqual(pending[0].upload["made_at"], .string(harness.clock.now.iso))
        // The question leaves the saved queue (its token is used up) and comes back at its new time.
        let queue = try await XCTUnwrapAsync(await harness.service.localQueue())
        XCTAssertFalse(queue.askable(now: harness.clock.now).contains { $0.id == item.id })
        XCTAssertEqual(queue.remaining(now: harness.clock.now), queue.bundle.remainingToday - 1)
        do {
            _ = try await harness.service.answerOffline(item, response: .object(["choice": .int(correct)]), confidence: .low)
            XCTFail("a second answer to the same question was taken")
        } catch {
            XCTAssertEqual(error, .invalid("This question was already answered on this phone; it comes back when it is due again."))
        }
    }

    func testAWrittenAnswerOfflineNeedsTheOnDeviceModelAndRecordsNothingWithout() async throws {
        let harness = try ReviewHarness(judge: FakeJudge(reply: nil, available: .unavailable("Apple Intelligence is turned off")))
        try await harness.saveFixtureBundle()
        let item = try await harness.item("blurb")
        let text = JSONValue.object(["text": .string(Array(repeating: "word", count: 12).joined(separator: " "))])
        do {
            _ = try await harness.service.answerOffline(item, response: text, confidence: .medium)
            XCTFail("graded without the model")
        } catch {
            XCTAssertEqual(error, .grading("Grading on this phone is unavailable: Apple Intelligence is turned off. Nothing was recorded."))
        }
        do {
            _ = try await harness.service.answerOffline(item, response: .object(["text": .string("too short")]), confidence: .medium)
            XCTFail("a short answer was taken")
        } catch {
            XCTAssertEqual(error, .invalid("Write at least 8 words before submitting (you wrote 2); this was not counted as an attempt."))
        }
        do {
            _ = try await harness.service.answerOffline(item, response: text, confidence: nil)
            XCTFail("an answer without a confidence was taken")
        } catch {
            XCTAssertEqual(error, .invalid("Say how sure you are first: Guessing, Fairly sure or Certain."))
        }
        let pending = try await harness.store.pending(harness.namespace)
        let answered = try await harness.store.answered(harness.namespace)
        XCTAssertEqual(pending, [])
        XCTAssertEqual(answered, [])
    }

    func testAWrittenAnswerIsGradedOnThePhoneWithTheRubric() async throws {
        let judge = FakeJudge(reply: JudgeReply(criteria: [
            .init(number: 1, justification: "Met.", met: true), .init(number: 2, justification: "Not met.", met: false),
            .init(number: 3, justification: "Not met.", met: false)
        ], feedback: "Add an example."))
        let harness = try ReviewHarness(judge: judge)
        try await harness.saveFixtureBundle()
        let item = try await harness.item("blurb")
        let text = JSONValue.object(["text": .string(Array(repeating: "word", count: 12).joined(separator: " "))])
        let result = try await harness.service.answerOffline(item, response: text, confidence: .medium)
        XCTAssertEqual(result.verdict, "partial")
        XCTAssertEqual(result.missing, ["Gives an example."])
        XCTAssertEqual(result.intervalDays, item.intervalDays, "a partial review keeps its interval")
        let pending = try await harness.store.pending(harness.namespace)
        XCTAssertEqual(pending.first?.upload["grader"], .string("device:apple-foundation-models"))
        XCTAssertEqual(pending.first?.upload["missing"], .array([.string("Gives an example.")]))
    }

    func testSyncUploadsLogsRejectionsAndTakesTheServersSchedule() async throws {
        let harness = try ReviewHarness()
        try await harness.saveFixtureBundle()
        harness.base.offline()
        let queue = try await XCTUnwrapAsync(await harness.service.localQueue())
        let choices = queue.askable(now: harness.clock.now).filter { $0.type == "multiple_choice" }.prefix(2)
        for item in choices {
            _ = try await harness.service.answerOffline(item, response: .object(["choice": .int(0)]), confidence: .medium)
        }
        let waiting = try await harness.store.pending(harness.namespace)
        let serverDue = harness.clock.now.adding(days: 9)
        let bundleData = try ReviewFixture.json()["review"]["bundle"].data()
        harness.base.transport.setHandler { request, _ in
            if request.url?.path() == "/api/offline/review" {
                return FakeTransport.answer(200, ["Content-Type": "application/json"], data: bundleData)
            }
            guard request.url?.path() == "/api/offline/review/answers" else {
                return FakeTransport.answer(404, [:], body: #"{"detail":"Not Found"}"#) // the renderer: not in this test
            }
            let body = try JSONValue(data: request.httpBody ?? Data())
            XCTAssertEqual(body["answers"].array, waiting.map(\.upload), "the queued items, unchanged and in order")
            let results: [JSONValue] = [
                .object(["client_id": waiting[0].upload["client_id"], "item_id": waiting[0].upload["item_id"], "status": "accepted",
                         "reason": .null, "detail": .null, "device_verdict": "failed", "regraded": true, "verdict_mismatch": true,
                         "recorded": .object(["verdict": "passed", "interval_days": .double(6.6), "due_at": .string(serverDue.iso)])]),
                .object(["client_id": waiting[1].upload["client_id"], "item_id": waiting[1].upload["item_id"], "status": "rejected",
                         "reason": "presentation_changed", "detail": "it was shown again since", "device_verdict": "passed",
                         "regraded": false, "verdict_mismatch": false, "recorded": .null])
            ]
            return FakeTransport.answer(200, ["Content-Type": "application/json"],
                                        data: JSONValue.object(["accepted": 1, "already_accepted": 0, "rejected": 1,
                                                                "results": .array(results)]).data())
        }
        let summary = await harness.service.sync()
        XCTAssertEqual(summary.uploaded, 1)
        XCTAssertEqual(summary.logged, 2)
        let left = try await harness.store.pending(harness.namespace)
        XCTAssertEqual(left, [], "every answer that was sent leaves the queue, the rejected one too (it would be rejected again)")
        let log = await harness.base.store.log(harness.namespace)
        XCTAssertEqual(log.map(\.kind), [.mismatch, .rejected])
        XCTAssertTrue(log[1].message.contains("answered elsewhere first"))
        XCTAssertNotNil(log[1].attempt, "the log says which answer it was")
        let answered = try await harness.store.answered(harness.namespace)
        XCTAssertEqual(answered.first { $0.itemID == waiting[0].itemID }?.dueAt, serverDue, "the server's schedule")
        XCTAssertTrue(harness.base.transport.requests.contains { $0.url?.path() == "/api/offline/review" },
                      "fetched again after the upload")
    }

    func testAFailedUploadKeepsTheAnswersAndSaysSo() async throws {
        let harness = try ReviewHarness()
        try await harness.saveFixtureBundle()
        let item = try await harness.item("multiple_choice")
        harness.base.offline()
        _ = try await harness.service.answerOffline(item, response: .object(["choice": .int(0)]), confidence: .medium)
        let offline = await harness.service.sync()
        XCTAssertTrue(offline.unreachable)
        XCTAssertEqual(offline.logged, 0, "offline, the queue waits quietly")
        harness.base.transport.setHandler { _, _ in FakeTransport.answer(500, [:], body: #"{"detail":"database is locked"}"#) }
        let failed = await harness.service.sync()
        XCTAssertEqual(failed.logged, 1)
        let pending = try await harness.store.pending(harness.namespace)
        XCTAssertEqual(pending.count, 1)
        let log = await harness.base.store.log(harness.namespace)
        XCTAssertTrue(log.last?.message.contains("database is locked") == true)
    }

    func testOnlineAnswersAndTheServersErrors() async throws {
        let harness = try ReviewHarness()
        try await harness.saveFixtureBundle()
        let item = try await harness.item("multiple_choice")
        let reply = try ReviewFixture.json()["review"]["answers"].array?.first?["reply"] ?? .null
        harness.base.transport.setHandler { request, _ in
            XCTAssertEqual(request.url?.path(), "/api/review/items/\(item.id)/answers")
            let body = try JSONValue(data: request.httpBody ?? Data())
            XCTAssertEqual(body, .object(["presentation": .string(item.presentation), "response": .object(["choice": 0]),
                                          "confidence": "high"]))
            return FakeTransport.answer(201, ["Content-Type": "application/json"], data: reply.data())
        }
        let result = try await harness.service.answerOnline(item, response: .object(["choice": 0]), confidence: .high)
        XCTAssertEqual(result.verdict, "passed")
        XCTAssertFalse(result.gradedOnPhone)
        let queue = try await XCTUnwrapAsync(await harness.service.localQueue())
        XCTAssertFalse(queue.askable(now: harness.clock.now).contains { $0.id == item.id }, "answered online: not asked offline")

        harness.base.transport.setHandler { _, _ in FakeTransport.answer(409, [:], body: #"{"detail":"answered elsewhere"}"#) }
        do {
            _ = try await harness.service.answerOnline(item, response: .object(["choice": 0]), confidence: .high)
            XCTFail("a 409 was taken")
        } catch {
            XCTAssertEqual(error, .refused(status: 409, detail: "answered elsewhere"))
        }
        harness.base.offline()
        do {
            _ = try await harness.service.answerOnline(item, response: .object(["choice": 0]), confidence: .high)
            XCTFail("unreachable was taken")
        } catch {
            guard case .unreachable = error else { return XCTFail("\(error)") }
        }
    }

    func testAFreshBundleKeepsOnlyTheAnswersItDoesNotCount() async throws {
        let harness = try ReviewHarness()
        let store = harness.store
        let namespace = harness.namespace
        let bundle = try ReviewFixture.bundle()
        let before = bundle.bundledAt.adding(days: -0.1)
        let after = bundle.bundledAt.adding(days: 0.1)
        func answer(_ id: Int, _ moment: Instant, pending: Bool) -> LocalReviewAnswer {
            LocalReviewAnswer(itemID: id, presentation: "p", slug: "s", madeAt: moment, verdict: "passed", intervalDays: 2,
                              dueAt: moment.adding(days: 2), pending: pending)
        }
        try await store.record(namespace, answer(1, before, pending: false), pending: nil)
        try await store.record(namespace, answer(2, before, pending: true), pending: nil)
        try await store.record(namespace, answer(3, after, pending: false), pending: nil)
        try await store.saveBundle(namespace, bundle)
        let kept = try await store.answered(namespace)
        XCTAssertEqual(kept.map(\.itemID), [2, 3], "the server counts an uploaded answer made before the bundle; not the others")
        // Signing out: the bundle goes, waiting answers stay.
        let waiting = PendingReviewAnswer(clientID: UUID(), itemID: 2, slug: "s", title: "S", madeAt: before, summary: "x", upload: .null)
        try await store.record(namespace, answer(2, before, pending: true), pending: waiting)
        try await store.forgetBundle(namespace)
        let gone = try await store.bundle(namespace)
        XCTAssertNil(gone)
        let left = try await store.pending(namespace)
        XCTAssertEqual(left, [waiting])
    }
}

final class LocalReviewQueueTests: XCTestCase {
    private let start = Instant(isoString: "2026-11-30T21:00:00+00:00")! // swiftlint:disable:this force_unwrapping

    func testItemsThatComeDueLaterAreOrderedConfidentFirstThenMostOverdueAndInterleaved() throws {
        let bundle = try ReviewFixture.bundle(items: [
            (1, "a", start.adding(days: -1), true, false),
            (2, "a", start.adding(days: -0.5), true, false),
            (3, "a", start.adding(days: 0.1), false, false),
            (4, "a", start.adding(days: 0.2), false, false),
            (5, "b", start.adding(days: 0.3), false, false),
            (6, "b", start.adding(days: 0.4), false, true),
            (7, "c", start.adding(days: 5), false, false)
        ])
        let queue = LocalReviewQueue(bundle: bundle, answered: [])
        XCTAssertEqual(queue.due(now: start).map(\.id), [1, 2])
        // A day later: those due when bundled first, as bundled; then the confident error,
        // then by due time, never two of one page in a row while another page has one.
        let later = start.adding(days: 1)
        XCTAssertEqual(queue.due(now: later).map(\.id), [1, 2, 6, 3, 5, 4])
        XCTAssertEqual(queue.nextDue(after: later), start.adding(days: 5))
        let answered = LocalReviewQueue(bundle: bundle, answered: [LocalReviewAnswer(
            itemID: 7, presentation: "token-7", slug: "c", madeAt: later, verdict: "passed", intervalDays: 2,
            dueAt: later.adding(days: 2), pending: true)])
        XCTAssertEqual(answered.nextDue(after: later.adding(days: 0.5)), later.adding(days: 2), "answered here: its new time")
    }

    func testTheDailyCapCountsAnswersMadeHereAndStartsAgainTheNextDay() throws {
        var raw = try ReviewFixture.bundle().raw.object ?? [:]
        raw["remaining_today"] = 2
        raw["cap"] = 3
        let bundle = try ReviewBundle(.object(raw))
        let bundledAt = bundle.bundledAt
        var queue = LocalReviewQueue(bundle: bundle, answered: [])
        XCTAssertEqual(queue.askable(now: bundledAt).count, 2)
        queue.answered = [LocalReviewAnswer(itemID: bundle.items[0].id, presentation: "", slug: "rev", madeAt: bundledAt, verdict: "passed",
                                            intervalDays: 1, dueAt: bundledAt.adding(days: 1), pending: true)]
        XCTAssertEqual(queue.remaining(now: bundledAt), 1)
        XCTAssertEqual(queue.askable(now: bundledAt).count, 1)
        // Tomorrow in Stockholm: the full cap again (yesterday's answer counts for yesterday).
        let tomorrow = queue.dayStart(of: bundledAt).adding(days: 1.2)
        XCTAssertEqual(queue.remaining(now: tomorrow), 3)
        XCTAssertEqual(queue.dayStart(of: bundledAt), bundle.dayStart, "the day starts at midnight in the bundle's zone")
    }
}

@MainActor
final class ReviewSessionTests: XCTestCase {
    func testOfflineTheSessionAsksTheSavedQuestionsAndEndsWithASummary() async throws {
        let harness = try ReviewHarness()
        try await harness.saveFixtureBundle()
        harness.base.offline()
        let session = ReviewSession(request: ReviewRequest(kind: .all), review: harness.service, offline: false)
        await session.start()
        guard case .phone = session.source else { return XCTFail("the unreachable server sends the session to the phone") }
        XCTAssertEqual(session.total, 10)
        XCTAssertEqual(session.position, 1)
        var answered = 0
        while case .asking(let item) = session.phase, answered < 20 {
            session.confidence = .medium
            if item.type == "blurb" {
                session.text = "too short"
                await session.submit()
                XCTAssertNotNil(session.problem, "a too short answer is refused, the question stays")
                XCTAssertEqual(session.currentItem, item)
                await session.skip()
                continue
            }
            session.choice = item.answerKey["correct_index"].int
            XCTAssertTrue(session.canSubmit)
            await session.submit()
            guard case .graded(_, let result) = session.phase else { return XCTFail("not graded: \(String(describing: session.problem))") }
            XCTAssertEqual(result.verdict, "passed")
            XCTAssertNotNil(session.lastVerdict)
            answered += 1
            await session.next()
        }
        XCTAssertEqual(session.phase, .summary)
        XCTAssertEqual(session.answered.count, answered)
        XCTAssertEqual(session.passedCount, answered)
        XCTAssertEqual(session.onPhoneCount, answered)
        XCTAssertNotNil(session.nextDue)
    }

    func testOnlineWithNothingDueItSaysWhenTheNextIsDue() async throws {
        let harness = try ReviewHarness()
        let next = Instant.now().adding(days: 2)
        harness.base.transport.setHandler { _, _ in
            FakeTransport.answer(200, ["Content-Type": "application/json"], data: JSONValue.object([
                "now": .string(Instant.now().iso), "tz": "Europe/Stockholm", "day_start": .string(Instant.now().iso), "cap": 20,
                "done_today": 3, "remaining_today": 17, "due": 0, "items_total": 4, "next_due_at": .string(next.iso),
                "concept": .null, "items": .array([])
            ]).data())
        }
        let session = ReviewSession(request: ReviewRequest(kind: .quick(minutes: 5)), review: harness.service, offline: false)
        await session.start()
        guard case .nothingDue(let text, let canExtend) = session.phase else { return XCTFail("\(session.phase)") }
        XCTAssertTrue(text.hasPrefix("Nothing is due now. The next question is due in 2 days ("), text)
        XCTAssertFalse(canExtend)
    }

    func testAQuickReviewFinishesTheQuestionOnScreenWhenTimeIsUp() async throws {
        let harness = try ReviewHarness()
        try await harness.saveFixtureBundle()
        var now = Date()
        let session = ReviewSession(request: ReviewRequest(kind: .quick(minutes: 3)), review: harness.service, offline: true,
                                    now: { now })
        await session.start()
        guard case .asking(let item) = session.phase else { return XCTFail("\(session.phase)") }
        now = now.addingTimeInterval(4 * 60) // the time runs out while answering
        session.choice = 0
        session.confidence = .low
        await session.submit()
        guard case .graded(let graded, _) = session.phase else { return XCTFail("the answer on screen is still graded") }
        XCTAssertEqual(graded, item)
        XCTAssertEqual(session.stepAfterVerdict, .finish)
        await session.next()
        XCTAssertEqual(session.phase, .summary)
        XCTAssertTrue(session.endedByTime)
    }
}

final class ReviewWordingTests: XCTestCase {
    func testTimeBox() {
        let start = Date(timeIntervalSince1970: 1_000)
        let box = TimeBox(start: start, minutes: 5)
        XCTAssertEqual(box.label(at: start), "5:00")
        XCTAssertEqual(box.label(at: start.addingTimeInterval(54.5)), "4:06")
        XCTAssertEqual(box.spoken(at: start.addingTimeInterval(55)), "4 minutes 5 seconds left")
        XCTAssertEqual(box.label(at: start.addingTimeInterval(400)), "0:00")
        XCTAssertTrue(box.isOver(at: start.addingTimeInterval(300)))
        XCTAssertEqual(box.used(at: start.addingTimeInterval(150)), 0.5)
        XCTAssertEqual(ReviewStep.afterVerdict(timeBox: box, now: start.addingTimeInterval(299), moreLeft: true), .next)
        XCTAssertEqual(ReviewStep.afterVerdict(timeBox: box, now: start.addingTimeInterval(300), moreLeft: true), .finish)
        XCTAssertEqual(ReviewStep.afterVerdict(timeBox: nil, now: start, moreLeft: false), .finish, "the queue ran out")
    }

    func testTheWebPagesWording() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        XCTAssertEqual(ReviewText.dueIn(now.addingTimeInterval(-5), now: now), "now")
        XCTAssertEqual(ReviewText.dueIn(now.addingTimeInterval(20), now: now), "in 1 minute")
        XCTAssertEqual(ReviewText.dueIn(now.addingTimeInterval(40 * 60), now: now), "in 40 minutes")
        XCTAssertEqual(ReviewText.dueIn(now.addingTimeInterval(5 * 3600), now: now), "in 5 hours")
        XCTAssertEqual(ReviewText.dueIn(now.addingTimeInterval(30 * 3600), now: now), "tomorrow")
        XCTAssertEqual(ReviewText.dueIn(now.addingTimeInterval(3 * 86400), now: now), "in 3 days")
        XCTAssertEqual(ReviewText.dueIn(now.addingTimeInterval(30 * 86400), now: now), "in 4 weeks")
        XCTAssertEqual(ReviewText.interval(1), "1 day")
        XCTAssertEqual(ReviewText.interval(6.6000000000000005), "6.6 days")
        XCTAssertEqual(ReviewText.interval(21), "3 weeks")
        XCTAssertEqual(ReviewText.schedule(verdict: "partial", intervalDays: 3),
                       "Partly right. This question comes back in 3 days, the same gap as last time.")
        XCTAssertEqual(ReviewText.nothingDue(itemsTotal: 5, due: 4, remainingToday: 0, cap: 20, nextDue: nil, now: now),
                       "You have done today's 20 reviews. 4 more questions are due: stop here, or keep going.")
    }

    func testWhatIsDrawnByTheWebRendererAndWhatVoiceOverReads() {
        XCTAssertFalse(RichTextKind.needsWebRenderer("Pick the **prime**."))
        XCTAssertTrue(RichTextKind.needsWebRenderer(#"Which is $\frac{a}{b}$?"#))
        XCTAssertTrue(RichTextKind.needsWebRenderer(#"Solve \(x^2 = 4\)"#))
        XCTAssertTrue(RichTextKind.needsWebRenderer("Steps:\n\n1. one\n2. two"))
        XCTAssertTrue(RichTextKind.needsWebRenderer("```\ncode\n```"))
        XCTAssertEqual(SpeakableText.from(#"Which is $\frac{a}{b}$ when $x^2 = 4$?"#), "Which is a over b when x squared equals 4 ?")
        XCTAssertEqual(SpeakableText.from(#"$\sqrt{x_i}$ and **bold**"#), "square root of x sub i and bold")
    }

    func testServerTimesAreRead() throws {
        let instant = try XCTUnwrap(Instant(isoString: "2026-11-29T21:03:52.874128+00:00"))
        XCTAssertEqual(instant.iso, "2026-11-29T21:03:52.874128+00:00")
        XCTAssertEqual(Instant(isoString: "2026-11-29T22:03:52+01:00")?.iso, "2026-11-29T21:03:52+00:00")
        XCTAssertNil(Instant(isoString: "2026-11-29 21:03"))
        XCTAssertEqual(instant.adding(days: 1).iso, "2026-11-30T21:03:52.874128+00:00")
    }
}

/// `XCTUnwrap` for a value an `await` produced.
func XCTUnwrapAsync<T>(_ value: T?, file: StaticString = #filePath, line: UInt = #line) async throws -> T {
    try XCTUnwrap(value, file: file, line: line)
}

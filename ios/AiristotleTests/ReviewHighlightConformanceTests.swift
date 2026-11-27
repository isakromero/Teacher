import XCTest
@testable import Airistotle

/// The phone's review grading and scheduling, and its highlight endpoints, against what the
/// real server answered, recorded by `ios/tools/make_review_highlight_fixtures.py` into
/// `Fixtures/review_highlight_conformance.json`.
enum ReviewFixture {
    static func json() throws -> JSONValue {
        let url = try XCTUnwrap(Bundle(for: ReviewConformanceTests.self).url(forResource: "review_highlight_conformance",
                                                                            withExtension: "json"))
        return try JSONValue(data: Data(contentsOf: url))
    }

    /// The review bundle with ten questions due (from four days after they were cleared).
    static func bundle() throws -> ReviewBundle {
        try ReviewBundle(try json()["review"]["bundle"])
    }

    /// A study-page bundle with its highlights set to `highlights`.
    static func pageBundle(_ raw: JSONValue, highlights: [JSONValue]) throws -> OfflineBundle {
        guard case .object(var fields) = raw else { throw XCTSkip("no bundle") }
        fields["highlights"] = .array(highlights)
        return try OfflineBundle(.object(fields))
    }
}

final class ReviewConformanceTests: XCTestCase {
    func testThePhoneGradesAndSchedulesEveryAnswerAsTheServerDid() async throws {
        let fixture = try ReviewFixture.json()["review"]
        let bundle = try ReviewFixture.bundle()
        let answers = try XCTUnwrap(fixture["answers"].array)
        XCTAssertEqual(answers.count, 5)
        for answer in answers {
            let item = try XCTUnwrap(bundle.items.first { $0.id == answer["item_id"].int })
            let label = "item \(item.id) (\(item.type))"
            let reply = answer["reply"]
            let confidence = ReviewConfidence(rawValue: answer["confidence"].string ?? "")
            let checked = try ReviewGrading.check(item, response: answer["response"], confidence: confidence,
                                                  defaultMinWords: bundle.defaultMinWords)
            let grade: Grade
            if let byKey = ReviewGrading.choiceGrade(checked, grading: bundle.grading) {
                grade = byKey
            } else {
                let judge = FakeJudge(reply: try XCTUnwrap(FakeJudge.reply(answer["judge"]), label))
                grade = try await BlurbGrader(judge: judge).grade(block: ReviewGrading.judgeBlock(item), answer: checked.text ?? "")
            }
            XCTAssertEqual(grade.verdict, reply["verdict"].string, label)
            XCTAssertEqual(grade.feedback, reply["feedback"].string, label)
            XCTAssertEqual(grade.missing, (reply["missing"].array ?? []).compactMap(\.string), label)
            if item.type == "blurb" { XCTAssertEqual(grade.details, reply["details"], "\(label): the judge's details") }
            // The schedule: the same interval, to the last bit, and the same next due time.
            let interval = bundle.schedule.interval(after: grade.verdict, from: item.intervalDays)
            XCTAssertEqual(interval, reply["interval_days"].number, label)
            let answeredAt = try XCTUnwrap(Instant(isoString: answer["answered_at"].string ?? ""))
            XCTAssertEqual(answeredAt.adding(days: interval).iso, reply["due_at"].string, label)
            // The answer shown afterwards is the bundle's key, in the order shown.
            XCTAssertEqual(item.answerKey, reply["revealed"], label)
        }
        // A second answer to the same presentation is the server's 409, shown as it is.
        let error = try XCTUnwrap(fixture["errors"].array?.first)
        XCTAssertEqual(error["status"].int, 409)
    }

    func testTheUploadIsShapedAsTheServerTakesItAndItsResultsAreTold() throws {
        let fixture = try ReviewFixture.json()["review_upload"]
        let bundle = try ReviewBundle(fixture["bundle"])
        let sent = try XCTUnwrap(fixture["answers"].array?.first)
        let item = try XCTUnwrap(bundle.items.first { $0.id == sent["item_id"].int })
        let checked = try ReviewGrading.check(item, response: sent["response"], confidence: .high, defaultMinWords: 20)
        let grade = try XCTUnwrap(ReviewGrading.choiceGrade(checked, grading: bundle.grading))
        let madeAt = try XCTUnwrap(Instant(isoString: sent["made_at"].string ?? ""))
        let clientID = try XCTUnwrap(UUID(uuidString: sent["client_id"].string ?? ""))
        XCTAssertEqual(ReviewGrading.upload(checked, grade: grade, clientID: clientID, madeAt: madeAt), sent,
                       "exactly what the server accepted")
        let results = try XCTUnwrap(fixture["first"]["results"].array)
        XCTAssertEqual(results.map { $0["status"].string }, ["accepted", "accepted", "rejected"])
        let pending = PendingReviewAnswer(clientID: clientID, itemID: item.id, slug: "up2", title: "Study 4", madeAt: madeAt,
                                          summary: "a review", upload: sent)
        XCTAssertEqual(ReviewService.logEntries(for: results[0], pending: pending), [], "an accepted answer needs no word")
        let rejected = ReviewService.logEntries(for: results[2], pending: pending)
        XCTAssertEqual(rejected.map(\.kind), [.rejected])
        XCTAssertTrue(rejected.first?.message.contains("answered elsewhere first") == true)
        XCTAssertTrue(rejected.first?.message.contains("already_answered") == true)
        // Sent again, an accepted answer comes back already accepted, with the same schedule.
        let again = try XCTUnwrap(fixture["again"]["results"].array?.first)
        XCTAssertEqual(again["status"].string, "already_accepted")
        XCTAssertEqual(again["recorded"], results[0]["recorded"])
    }

    func testTheSavedQueueAsksWhatTheServerWouldAsk() throws {
        let bundle = try ReviewFixture.bundle()
        let queue = LocalReviewQueue(bundle: bundle, answered: [])
        let bundledAt = bundle.bundledAt
        // Everything due when bundled, in the bundle's (the queue's) order.
        XCTAssertEqual(queue.due(now: bundledAt).map(\.id), bundle.items.filter(\.dueWhenBundled).map(\.id))
        XCTAssertEqual(queue.remaining(now: bundledAt), bundle.remainingToday)
        XCTAssertEqual(queue.askable(now: bundledAt).count, min(bundle.items.count, bundle.remainingToday))
    }
}

final class HighlightConformanceTests: XCTestCase {
    /// `created_at` and `updated_at` set aside (the phone makes its highlights at other moments).
    private func withoutTimes(_ value: JSONValue) -> JSONValue {
        switch value {
        case .object(var fields):
            for key in ["created_at", "updated_at"] where fields[key] != nil { fields[key] = .string("<time>") }
            return .object(fields.mapValues(withoutTimes))
        case .array(let items):
            return .array(items.map(withoutTimes))
        default:
            return value
        }
    }

    /// `text` with every phone id put back as the server's id for the same highlight.
    private func asServer(_ text: String, _ ids: [String: String]) -> String {
        ids.reduce(text) { $0.replacingOccurrences(of: $1.key, with: $1.value) }
    }

    func testEveryHighlightRequestIsAnsweredOfflineAsTheServerDid() async throws {
        let fixture = try ReviewFixture.json()["highlights"]
        let harness = OfflineHarness()
        try await harness.save(try ReviewFixture.pageBundle(fixture["bundle"], highlights: []))
        harness.offline()
        let serverNames = (fixture["names"].object ?? [:]).compactMapValues(\.string) // h1 -> the server's id
        var phoneToServer: [String: String] = [:]
        var serverToPhone: [String: String] = [:]
        func phoneText(_ text: String) -> String {
            var out = text
            for (name, server) in serverNames { out = out.replacingOccurrences(of: "{\(name)}", with: serverToPhone[server] ?? server) }
            return out
        }
        let steps = try XCTUnwrap(fixture["steps"].array)
        for (index, step) in steps.enumerated() {
            let method = step["method"].string ?? ""
            let label = "step \(index + 1): \(method) \(step["path"].string ?? "") \(step["body"].data().text.prefix(80))"
            let body = step["body"].isNull ? nil : Data(phoneText(step["body"].data().text).utf8)
            let reply = await harness.gateway.reply(to: GatewayRequest(
                method: method, target: phoneText(step["path"].string ?? ""), headers: ["Content-Type": "application/json"], body: body
            ))
            XCTAssertEqual(reply.status, step["status"].int, label)
            XCTAssertEqual(reply.source, .local("saved-page"), label)
            let data = try await reply.collectBody()
            let expected = step["json"]
            guard !expected.isNull else {
                XCTAssertTrue(data.isEmpty, "\(label): no body")
                continue
            }
            if reply.status == 201, let made = try JSONValue(data: data)["id"].string, let server = expected["id"].string {
                phoneToServer[made] = server
                serverToPhone[server] = made
            }
            let mine = try JSONValue(data: Data(asServer(data.text, phoneToServer).utf8))
            XCTAssertEqual(withoutTimes(mine), withoutTimes(expected), label)
        }
        XCTAssertEqual(phoneToServer.count, 5, "every highlight made was matched")
        // Each change is queued for the upload, in order. The removed highlight was made on this
        // phone too, so it is simply forgotten with its create.
        let page = await harness.page("hl")
        let changes = try XCTUnwrap(page?.highlights.changes)
        XCTAssertEqual(changes.map(\.op), ["create", "create", "create", "create", "update_note", "update_note", "update_note",
                                           "update_note"])
        XCTAssertFalse(changes.contains { $0.highlightID == serverToPhone[serverNames["h4"] ?? ""] },
                       "a highlight made and removed offline is never sent")
    }

    func testChangesMadeOnThePhoneAreWhatTheServerApplies() throws {
        let fixture = try ReviewFixture.json()["highlight_upload"]
        let sent = try XCTUnwrap(fixture["changes"].array)
        var engine = HighlightEngine(slug: "up", blocks: fixture["blocks"].array ?? [],
                                     saved: SavedHighlights(list: fixture["before"]["highlights"].array ?? []))
        func time(_ index: Int) throws -> Instant { try XCTUnwrap(Instant(isoString: sent[index]["made_at"].string ?? "")) }
        let first = sent[0]
        _ = try engine.create(.object(["id": first["id"], "block_key": first["block_key"], "quote": first["quote"],
                                       "prefix": first["prefix"], "suffix": first["suffix"], "start": first["start"],
                                       "end": first["end"]]), now: try time(0))
        _ = try engine.updateNote(id: sent[1]["id"].string ?? "", .object(["note": sent[1]["note"]]), now: try time(1))
        let online = sent[3]
        _ = try engine.updateNote(id: online["id"].string ?? "", .object(["note": online["note"]]), now: try time(3))
        XCTAssertEqual(engine.saved.changes.map(\.upload), [sent[0], sent[1], sent[3]], "the upload items, exactly")
        // An unknown block is refused on the phone (the server would reject it on upload).
        XCTAssertThrowsError(try engine.create(.object(["block_key": "b99", "quote": "nowhere", "start": 0, "end": 7]), now: try time(4)))
        // A highlight made and removed offline never reaches the server.
        let made = try engine.create(.object(["block_key": "b03", "quote": "More prose", "start": 0, "end": 10]), now: try time(2))
        try engine.delete(id: made.highlight["id"].string ?? "", now: try time(5))
        XCTAssertEqual(engine.saved.changes.count, 3)
        // The phone's highlight is the server's after the upload, times included.
        let mine = try XCTUnwrap(engine.saved.list?.first { $0["id"] == first["id"] })
        let theirs = try XCTUnwrap(fixture["answer"]["highlights"].array?.first { $0["id"] == first["id"] })
        XCTAssertEqual(mine, theirs)
    }

    func testTheUploadsAnswerReplacesTheListAndEveryChangeThatDidNotApplyIsTold() async throws {
        let fixture = try ReviewFixture.json()["highlight_upload"]
        let harness = OfflineHarness()
        var raw = try OfflineFixture.studyBundle().raw.object ?? [:]
        raw["artifact"] = .string("up")
        raw["blocks"] = .array(fixture["blocks"].array ?? [])
        raw["highlights"] = fixture["before"]["highlights"]
        try await harness.save(try OfflineBundle(.object(raw)))
        let changes = (fixture["changes"].array ?? []).map { change in
            HighlightChange(changeID: UUID(), highlightID: change["id"].string ?? "", op: change["op"].string ?? "",
                            madeAt: Instant(isoString: change["made_at"].string ?? "") ?? .now(), upload: change,
                            quote: change["quote"].string ?? "")
        }
        try await harness.store.saveHighlights(harness.namespace, "up", SavedHighlights(
            list: fixture["before"]["highlights"].array, removed: [], changes: changes))
        let answer = fixture["answer"]
        harness.transport.setHandler { request, _ in
            XCTAssertEqual(request.url?.path(), "/api/offline/artifacts/up/highlights")
            let body = try JSONValue(data: request.httpBody ?? Data())
            XCTAssertEqual(body["changes"], fixture["changes"])
            return FakeTransport.answer(200, ["Content-Type": "application/json"], data: answer.data())
        }
        let summary = await harness.service.syncHighlights()
        XCTAssertEqual(summary.uploaded, 4)
        let page = await harness.page("up")
        XCTAssertEqual(page?.highlights.changes, [])
        XCTAssertEqual(page?.highlights.list, answer["highlights"].array, "the server's list replaces the phone's")
        let log = await harness.store.log(harness.namespace)
        XCTAssertEqual(log.map(\.kind), [.mismatch, .rejected], "the stale note and the unknown block are both told")
        XCTAssertTrue(log.first?.message.contains("changed later elsewhere") == true)
        XCTAssertTrue(log.last?.message.contains("unknown_block") == true)
    }
}

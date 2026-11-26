import XCTest
@testable import Airistotle

/// The reading engine against what the real server answered, recorded by
/// `ios/tools/make_offline_fixtures.py` into `Fixtures/offline_conformance.json`.
final class OfflineConformanceTests: XCTestCase {
    private func fixture() throws -> JSONValue {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "offline_conformance", withExtension: "json"))
        return try JSONValue(data: Data(contentsOf: url))
    }

    /// `created_at` set to null everywhere (the phone's attempts are made at other moments).
    private func withoutTimes(_ value: JSONValue) -> JSONValue {
        switch value {
        case .object(var fields):
            if fields["created_at"] != nil { fields["created_at"] = .null }
            return .object(fields.mapValues(withoutTimes))
        case .array(let items):
            return .array(items.map(withoutTimes))
        default:
            return value
        }
    }

    private func assertViews(_ engine: ReadingEngine, _ expected: JSONValue, times: Bool, _ label: String,
                             file: StaticString = #filePath, line: UInt = #line) throws {
        let strip = { (value: JSONValue) in times ? value : self.withoutTimes(value) }
        XCTAssertEqual(strip(engine.blocksView()), strip(expected["blocks"]), "\(label): GET .../blocks", file: file, line: line)
        for (key, responses) in expected["responses"].object ?? [:] {
            XCTAssertEqual(strip(try engine.responsesView(key: key)), strip(responses), "\(label): GET .../\(key)/responses",
                           file: file, line: line)
        }
    }

    func testEveryReadAndSubmissionMatchesTheServer() async throws {
        let pages = try XCTUnwrap(try fixture()["pages"].array)
        XCTAssertEqual(pages.count, 2)
        var checkedSteps = 0
        for page in pages {
            var engine = ReadingEngine(bundle: try OfflineBundle(page["bundle"]))
            let slug = page["slug"].string ?? "?"
            try assertViews(engine, page["initial"], times: true, "\(slug) before any answer")
            for (index, step) in (page["steps"].array ?? []).enumerated() {
                let label = "\(slug) step \(index + 1) (\(step["key"].string ?? ""))"
                let status = step["status"].int ?? 0
                let submission: ReadingEngine.Submission
                do {
                    submission = try engine.prepare(key: step["key"].string ?? "", body: step["body"], onlyOpenGates: false)
                } catch {
                    XCTAssertEqual(error, EngineError(status: status, detail: step["detail"].string ?? ""), label)
                    checkedSteps += 1
                    continue
                }
                XCTAssertEqual(status, 201, "\(label): the phone accepted what the server refused: \(step["detail"])")
                let grade: Grade
                if let deterministic = engine.deterministicGrade(submission) {
                    grade = deterministic
                } else {
                    let judge = FakeJudge(reply: try XCTUnwrap(FakeJudge.reply(step["judge"]), label))
                    grade = try await BlurbGrader(judge: judge).grade(block: submission.block, answer: submission.answerText ?? "")
                }
                let recorded = engine.record(submission, grade: grade, madeAt: .now())
                XCTAssertEqual(recorded.reply, step["reply"], "\(label): the reply")
                engine = recorded.engine
                try assertViews(engine, step, times: false, label)
                checkedSteps += 1
            }
        }
        XCTAssertEqual(checkedSteps, 23)
    }

    func testAttemptsMadeOnThePhoneReadLikeTheServerAfterTheUpload() throws {
        let upload = try fixture()["upload"]
        let before = ReadingEngine(bundle: try OfflineBundle(upload["bundle_before"]))
        var engine = before
        for item in upload["items"].array ?? [] {
            let key = item["key"].string ?? ""
            let body = JSONValue.object(["response": item["response"], "confidence": item["confidence"]])
            let submission = try engine.prepare(key: key, body: body, onlyOpenGates: true)
            let grade = engine.deterministicGrade(submission) ?? Grade(
                verdict: item["verdict"].string ?? "", feedback: item["feedback"].string,
                missing: (item["missing"].array ?? []).compactMap(\.string), details: item["details"], grader: item["grader"].string ?? ""
            )
            let madeAt = try XCTUnwrap(Instant(iso: item["made_at"].string ?? ""))
            engine = engine.record(submission, grade: grade, madeAt: madeAt).engine
        }
        // The phone's own view, times included, is the server's after the upload...
        try assertViews(engine, upload["after"], times: true, "the phone before uploading")
        // ...and the bundle the upload returns reads the same.
        let answer = upload["answer"]
        XCTAssertEqual(answer["accepted"].int, 2)
        try assertViews(ReadingEngine(bundle: try OfflineBundle(answer["bundle"])), upload["after"], times: true, "the returned bundle")
        // The recorded attempts are what the phone recorded.
        let rows = engine.bundle.blocks.flatMap { $0["responses"].array ?? [] }
        XCTAssertEqual(answer["results"].array?.map { $0["recorded"] }, rows)
    }
}

/// A judge that answers with a fixed reply (or fails).
struct FakeJudge: BlurbJudge {
    var reply: JudgeReply?
    var failure: JudgeFailure?
    var available: JudgeAvailability = .available
    let name = "apple-foundation-models"
    let modelName = "apple-on-device-system-language-model"

    func availability() -> JudgeAvailability { available }

    func criteria(_ request: JudgeRequest) async throws(JudgeFailure) -> [JudgeReply.Criterion] {
        if let failure { throw failure }
        guard let reply else { throw .other("no scripted reply") }
        return reply.criteria
    }

    func feedback(_ request: FeedbackRequest) async throws(JudgeFailure) -> String {
        guard let reply else { throw .other("no scripted reply") }
        return reply.feedback
    }

    static func reply(_ value: JSONValue) -> JudgeReply? {
        guard let criteria = value["criteria"].array, let feedback = value["feedback"].string else { return nil }
        return JudgeReply(criteria: criteria.map {
            .init(number: $0["number"].int ?? 0, justification: $0["justification"].string ?? "", met: $0["met"].bool ?? false)
        }, feedback: feedback)
    }
}

extension Instant {
    /// `YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00`, as the server writes UTC times (tests only).
    init?(iso: String) {
        let pattern = #"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?\+00:00$"#
        guard let match = iso.wholeMatch(of: try! Regex(pattern)) else { return nil } // swiftlint:disable:this force_try
        let numbers = (1...6).map { Int(match.output[$0].substring ?? "") ?? 0 }
        var parts = DateComponents()
        (parts.year, parts.month, parts.day, parts.hour, parts.minute, parts.second) =
            (numbers[0], numbers[1], numbers[2], numbers[3], numbers[4], numbers[5])
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC") ?? .gmt
        guard let date = calendar.date(from: parts) else { return nil }
        let fraction = match.output[7].substring.map { String($0).padding(toLength: 6, withPad: "0", startingAt: 0) } ?? "0"
        self.init(micros: Int64(date.timeIntervalSince1970) * 1_000_000 + (Int64(fraction) ?? 0))
    }
}

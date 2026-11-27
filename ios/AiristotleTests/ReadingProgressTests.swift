import XCTest
@testable import Airistotle

/// Reading a study page's progress from the replies the page receives (`ReadingProgress`),
/// online and for a saved page answered on the phone, and the app congratulating on it.
final class ReadingProgressTests: XCTestCase {
    private func request(_ method: String, _ target: String, body: String? = nil) -> GatewayRequest {
        GatewayRequest(method: method, target: target, headers: ["Accept": "application/json"], body: body.map { Data($0.utf8) })
    }

    private func reports(_ recorder: EventRecorder) -> [ReadingReport.Kind] {
        recorder.events.compactMap { event in if case .readingProgress(let report) = event { report.kind } else { nil } }
    }

    func testWhichRequestsAreRead() {
        XCTAssertEqual(ReadingProgress.watched(request("GET", "/api/artifacts/waves/blocks")), .blocks(slug: "waves"))
        XCTAssertEqual(ReadingProgress.watched(request("POST", "/api/artifacts/waves/blocks/b02/responses")), .submit(slug: "waves"))
        XCTAssertEqual(ReadingProgress.watched(request("GET", "/api/artifacts/waves")), .artifact(slug: "waves"))
        XCTAssertEqual(ReadingProgress.watched(request("GET", "/api/artifacts/a%20b")), .artifact(slug: "a b"))
        XCTAssertNil(ReadingProgress.watched(request("GET", "/api/artifacts/waves/blocks/b02/responses")))
        XCTAssertNil(ReadingProgress.watched(request("POST", "/api/artifacts/waves/chat/blocks/3/responses")), "a tutor's exercise")
        XCTAssertNil(ReadingProgress.watched(request("DELETE", "/api/artifacts/waves")))
        XCTAssertNil(ReadingProgress.watched(request("GET", "/api/artifacts")))
        XCTAssertNil(ReadingProgress.watched(request("GET", "/artifacts/waves")), "the page itself")
        XCTAssertNil(ReadingProgress.watched(request("GET", "/api/labs/waves")), "labs carry no such signal")
    }

    func testWhatARepliesSays() {
        func report(_ watched: ReadingProgress.Watched, _ status: Int, _ body: String,
                    source: ReplySource = .server) -> ReadingReport.Kind? {
            ReadingProgress.report(watched, namespace: "ns", status: status, source: source, body: Data(body.utf8))?.kind
        }
        XCTAssertEqual(report(.blocks(slug: "w"), 200, #"{"artifact":"w","complete":false,"blocks":[]}"#), .state(complete: false))
        XCTAssertEqual(report(.submit(slug: "w"), 201, #"{"passed":true,"verdict":"passed","complete":true}"#),
                       .answered(complete: true))
        XCTAssertEqual(report(.artifact(slug: "w"), 200, #"{"slug":"w","title":"Waves","progress":{"complete":false}}"#),
                       .title("Waves"))
        XCTAssertEqual(report(.submit(slug: "w"), 201, #"{"complete":true}"#, source: .local(ReadingEngine.sourceName)),
                       .answered(complete: true), "the phone's reading engine")
        XCTAssertNil(report(.blocks(slug: "w"), 200, #"{"complete":false}"#, source: .cache(storedAt: Date())), "an old copy")
        XCTAssertEqual(report(.artifact(slug: "w"), 200, #"{"slug":"w","title":"Waves"}"#, source: .cache(storedAt: Date())),
                       .title("Waves"), "an old copy still knows the title")
        XCTAssertNil(report(.submit(slug: "w"), 200, #"{"complete":true}"#), "not 201")
        XCTAssertNil(report(.submit(slug: "w"), 201, #"{"complete":"yes"}"#))
        XCTAssertNil(report(.blocks(slug: "w"), 409, #"{"detail":"not ready"}"#))
        XCTAssertNil(report(.artifact(slug: "w"), 200, #"{"slug":"other","title":"Waves"}"#))
        XCTAssertNil(report(.artifact(slug: "w"), 200, "not json"))
    }

    func testTheServersRepliesAreReadOnceThePageHasThem() async throws {
        let transport = FakeTransport { request, _ in
            let path = request.url?.path ?? ""
            if path.hasSuffix("/blocks") {
                return FakeTransport.answer(200, body: #"{"artifact":"waves","complete":false,"blocks":[]}"#)
            }
            if path.hasSuffix("/responses") {
                return FakeTransport.answer(201, body: #"{"passed":true,"verdict":"passed","complete":true}"#)
            }
            return FakeTransport.answer(200, body: #"{"slug":"waves","title":"Waves and superposition"}"#)
        }
        let recorder = EventRecorder()
        let gateway = Gateway(transport: transport, cache: ResponseCache(root: TestFiles.folder()), events: recorder.sink)
        gateway.connection = .test
        _ = try await gateway.reply(to: request("GET", "/api/artifacts/waves")).collectBody()
        let blocks = await gateway.reply(to: request("GET", "/api/artifacts/waves/blocks"))
        XCTAssertEqual(reports(recorder), [.title("Waves and superposition")], "not before the page read the reply")
        let body = try await blocks.collectBody()
        XCTAssertEqual(body.text, #"{"artifact":"waves","complete":false,"blocks":[]}"#, "passed on unchanged")
        _ = try await gateway.reply(to: request("POST", "/api/artifacts/waves/blocks/b02/responses", body: "{}")).collectBody()
        XCTAssertEqual(reports(recorder), [.title("Waves and superposition"), .state(complete: false), .answered(complete: true)])
        XCTAssertTrue(recorder.events.contains(.answerGraded(.passed)), "the grade is still read")
        let namespaces = Set(recorder.events.compactMap { event in
            if case .readingProgress(let report) = event { report.namespace } else { nil }
        })
        XCTAssertEqual(namespaces, [GatewayConnection.test.cacheNamespace])
    }

    /// The saved page without its written answer: a multiple choice (b02) and a prediction (b05).
    private func choicesOnly() throws -> OfflineBundle {
        let raw = try OfflineFixture.studyBundle().raw
        guard case .object(var top) = raw, case .array(let blocks) = raw["blocks"] else { throw XCTSkip("unexpected fixture") }
        top["blocks"] = .array(blocks.filter { $0["key"].string != "b04" })
        return try OfflineBundle(.object(top))
    }

    func testASavedPageFinishedOfflineIsReadTheSameWay() async throws {
        let harness = OfflineHarness()
        let bundle = try choicesOnly()
        let artifact = #"{"slug":"study","title":"Study 1","progress":{"complete":false}}"#
        try await harness.save(bundle, files: ["/api/artifacts/study": artifact])
        harness.offline()
        _ = try await harness.get("/api/artifacts/study").collectBody()
        _ = try await harness.get("/api/artifacts/study/blocks").collectBody()
        _ = try await harness.submit("b02", #"{"response": {"choice": 1}, "confidence": "high"}"#).collectBody()
        _ = try await harness.submit("b05", #"{"response": {"choice": 0}}"#).collectBody()
        XCTAssertEqual(reports(harness.recorder), [.title("Study 1"), .state(complete: false), .answered(complete: false),
                                                   .answered(complete: true)])

        var watcher = PageCompletionWatcher()
        watcher.load(harness.namespace, slugs: [])
        let found = harness.recorder.events.compactMap { event -> PageCompletionWatcher.Completion? in
            if case .readingProgress(let report) = event { watcher.observe(report) } else { nil }
        }
        XCTAssertEqual(found, [.init(namespace: harness.namespace, slug: "study", title: "Study 1")])
    }

    @MainActor
    func testTheAppCongratulatesFromTheGatewaysEvents() throws {
        let defaults = UserDefaults(suiteName: "ReadingProgressTests-\(UUID().uuidString)")! // swiftlint:disable:this force_unwrapping
        let model = AppModel(transport: FakeTransport(), store: EphemeralSessionStore(),
                             cache: ResponseCache(root: TestFiles.folder()), defaults: defaults)
        model.mascot.setAvailable(.browser, true)
        let namespace = GatewayConnection.test.cacheNamespace
        model.handle(.readingProgress(ReadingReport(namespace: namespace, slug: "waves", kind: .title("Waves and superposition"))))
        model.handle(.readingProgress(ReadingReport(namespace: namespace, slug: "waves", kind: .state(complete: false))))
        XCTAssertNil(model.mascot.current)
        model.handle(.readingProgress(ReadingReport(namespace: namespace, slug: "waves", kind: .answered(complete: true))))
        XCTAssertEqual(model.mascot.current?.lines.first, "You finished Waves and superposition.")
    }
}

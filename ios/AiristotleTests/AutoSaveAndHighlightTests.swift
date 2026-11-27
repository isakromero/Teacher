import XCTest
@testable import Airistotle

final class AutoSavePolicyTests: XCTestCase {
    private let wifi = NetworkConditions(satisfied: true, expensive: false, constrained: false)
    private let cellular = NetworkConditions(satisfied: true, expensive: true, constrained: false)
    private let lowData = NetworkConditions(satisfied: true, expensive: false, constrained: true)

    func testTheGate() {
        XCTAssertEqual(AutoSaveGate.decide(enabled: true, wifiOnly: true, network: wifi), .allowed)
        XCTAssertEqual(AutoSaveGate.decide(enabled: true, wifiOnly: true, network: cellular), .waitingForWiFi)
        XCTAssertEqual(AutoSaveGate.decide(enabled: true, wifiOnly: false, network: cellular), .allowed)
        XCTAssertEqual(AutoSaveGate.decide(enabled: true, wifiOnly: false, network: lowData), .lowDataMode, "never in Low Data Mode")
        XCTAssertEqual(AutoSaveGate.decide(enabled: false, wifiOnly: false, network: wifi), .off)
        XCTAssertEqual(AutoSaveGate.decide(enabled: true, wifiOnly: false, network: .unknown), .noNetwork)
        XCTAssertNotNil(AutoSaveGate.lowDataMode.status)
        XCTAssertNil(AutoSaveGate.off.status)
    }

    func testWhatIsSavedAndWhatIsEvicted() {
        let now = Date()
        let policy = AutoSavePolicy(cap: 2, refreshAfter: 3600)
        let candidates = ["a", "b", "c", "d"].map { AutoSaveCandidate(slug: $0, title: $0.uppercased()) }
        let saved = [
            AutoSavePolicy.Saved(slug: "a", automatic: false, savedAt: now.addingTimeInterval(-9999), hasPendingWork: false),
            AutoSavePolicy.Saved(slug: "b", automatic: true, savedAt: now.addingTimeInterval(-60), hasPendingWork: false),
            AutoSavePolicy.Saved(slug: "c", automatic: true, savedAt: now.addingTimeInterval(-7200), hasPendingWork: false)
        ]
        // "a" was saved by hand: neither saved over nor counted; "b" is fresh; "c" is due again.
        XCTAssertEqual(policy.toSave(candidates + [candidates[2]], saved: saved, now: now).map(\.slug), ["c"])
        XCTAssertEqual(policy.toSave(candidates, saved: [], now: now).map(\.slug), ["a", "b"], "at most the cap")

        let many = [
            AutoSavePolicy.Saved(slug: "hand", automatic: false, savedAt: now.addingTimeInterval(-99999), hasPendingWork: false),
            AutoSavePolicy.Saved(slug: "old", automatic: true, savedAt: now.addingTimeInterval(-5000), hasPendingWork: false),
            AutoSavePolicy.Saved(slug: "older", automatic: true, savedAt: now.addingTimeInterval(-6000), hasPendingWork: true),
            AutoSavePolicy.Saved(slug: "new", automatic: true, savedAt: now, hasPendingWork: false),
            AutoSavePolicy.Saved(slug: "newer", automatic: true, savedAt: now.addingTimeInterval(10), hasPendingWork: false)
        ]
        XCTAssertEqual(policy.toEvict(many), ["old", "new"],
                       "oldest first, never one with work waiting, never one saved by hand")
        XCTAssertEqual(policy.toEvict(Array(many.prefix(2))), [])
    }

    func testPagesInProgressComeFromContinueStudying() throws {
        let value = try JSONValue(data: Data("""
        {"items": [
          {"artifact": {"slug": "a", "kind": "study_page", "title": "A", "status": "ready", "progress": {"complete": false}},
           "url": "/artifacts/a", "last_studied_at": "2026-11-26T10:00:00+00:00", "active_seconds": 60},
          {"artifact": {"slug": "lab", "kind": "lab", "title": "Lab", "status": "ready", "progress": {"complete": false}},
           "url": "/labs/lab", "last_studied_at": "2026-11-26T09:00:00+00:00", "active_seconds": 60},
          {"artifact": {"slug": "done", "kind": "study_page", "title": "Done", "status": "ready", "progress": {"complete": true}},
           "url": "/artifacts/done", "last_studied_at": "2026-11-26T08:00:00+00:00", "active_seconds": 60},
          {"artifact": {"slug": "gen", "kind": "study_page", "title": "Gen", "status": "generating", "progress": {"complete": false}},
           "url": "/artifacts/gen", "last_studied_at": "2026-11-26T07:00:00+00:00", "active_seconds": 60}
        ]}
        """.utf8))
        let found = AutoSaveCandidate.fromContinue(value)
        XCTAssertEqual(found.inProgress, [AutoSaveCandidate(slug: "a", title: "A")])
        XCTAssertEqual(found.complete, ["done"])
    }
}

final class AutoSaverTests: XCTestCase {
    /// A server with study pages `slugs` (the fixture's page under each name) and a continue list.
    private func serve(_ harness: OfflineHarness, slugs: [String], missing: Set<String> = []) throws {
        var site: [String: (Int, Data)] = [:]
        for slug in slugs where !missing.contains(slug) {
            var bundle = try OfflineFixture.studyBundle().raw.object ?? [:]
            bundle["artifact"] = .string(slug)
            bundle["title"] = .string(slug.uppercased())
            bundle["assets"] = .array([])
            site["/api/offline/artifacts/\(slug)"] = (200, JSONValue.object(bundle).data())
            site["/artifacts/\(slug)"] = (200, Data("<html></html>".utf8))
            for target in StudyPageRequests.targets(slug: slug, timeZone: TimeZone.current.identifier) {
                site[target] = (200, Data(target.hasSuffix("/chat/threads") ? "[]".utf8 : "{}".utf8))
            }
        }
        let items = slugs.map { slug -> JSONValue in
            .object(["artifact": .object(["slug": .string(slug), "kind": "study_page", "title": .string(slug.uppercased()),
                                          "status": "ready", "progress": .object(["complete": false])]),
                     "url": .string("/artifacts/\(slug)")])
        }
        let continued = JSONValue.object(["items": .array(items)]).data()
        let pages = site
        harness.transport.setHandler { request, _ in
            let target = (request.url?.path(percentEncoded: true) ?? "") + (request.url?.query(percentEncoded: true).map { "?" + $0 } ?? "")
            if target.hasPrefix("/api/history/continue?limit=") {
                return FakeTransport.answer(200, ["Content-Type": "application/json"], data: continued)
            }
            guard let (status, body) = pages[target] else { return FakeTransport.answer(404, [:], body: #"{"detail":"Not Found"}"#) }
            return FakeTransport.answer(status, ["Content-Type": "application/json"], data: body)
        }
    }

    private func saver(_ harness: OfflineHarness, network: NetworkConditions, cap: Int = 5) -> AutoSaver {
        AutoSaver(service: harness.service, preferences: OfflinePreferences(defaults: harness.defaults),
                  policy: AutoSavePolicy(cap: cap), network: { network })
    }

    func testThePagesBeingStudiedAreSavedAutomaticallyAndFailuresAreNoted() async throws {
        let harness = OfflineHarness()
        try serve(harness, slugs: ["one", "two"], missing: ["two"])
        let gate = await saver(harness, network: NetworkConditions(satisfied: true, expensive: false, constrained: false))
            .run(current: "one")
        XCTAssertEqual(gate, .allowed)
        let one = await harness.page("one")
        XCTAssertEqual(one?.record.automatic, true)
        let two = await harness.page("two")
        XCTAssertNil(two)
        let notes = await harness.store.autoSaveNotes(harness.namespace)
        XCTAssertEqual(notes.map(\.slug), ["two"])
        XCTAssertTrue(notes.first?.message.contains("404") == true, notes.first?.message ?? "")
        // Saved by hand later, the page is the reader's: no longer automatic.
        _ = try await harness.service.save(slug: "one")
        let manual = await harness.page("one")
        XCTAssertEqual(manual?.record.automatic, false)
    }

    func testNothingIsSavedInLowDataModeOrOverCellularWhenWiFiOnly() async throws {
        let harness = OfflineHarness()
        try serve(harness, slugs: ["one"])
        let lowData = await saver(harness, network: NetworkConditions(satisfied: true, expensive: false, constrained: true))
            .run(current: "one")
        XCTAssertEqual(lowData, .lowDataMode)
        let cellular = await saver(harness, network: NetworkConditions(satisfied: true, expensive: true, constrained: false))
            .run(current: "one")
        XCTAssertEqual(cellular, .waitingForWiFi)
        XCTAssertTrue(harness.transport.requests.isEmpty, "not even asked")
        let pages = await harness.store.pages(harness.namespace)
        XCTAssertEqual(pages, [])
    }

    func testTheOldestAutomaticSaveGoesWhenThereAreTooMany() async throws {
        let harness = OfflineHarness()
        try serve(harness, slugs: ["one", "two", "three"])
        let wifi = NetworkConditions(satisfied: true, expensive: false, constrained: false)
        _ = await saver(harness, network: wifi, cap: 2).run(current: nil)
        var slugs = await harness.store.pages(harness.namespace).map(\.record.slug)
        XCTAssertEqual(Set(slugs), ["one", "two"])
        // "three" is now studied first: it is saved, the oldest automatic save ("one") goes.
        try serve(harness, slugs: ["three", "two"])
        _ = await saver(harness, network: wifi, cap: 2).run(current: nil)
        slugs = await harness.store.pages(harness.namespace).map(\.record.slug)
        XCTAssertEqual(Set(slugs), ["two", "three"])
    }

    func testOldRecordsAreSavedByHand() throws {
        let json = #"{"slug":"x","title":"X","savedAt":"2026-11-20T10:00:00Z","version":"v","deletedOnServer":false,"bytes":1,"files":{}}"#
        let record = try SavedStore.decoder.decode(SavedPageRecord.self, from: Data(json.utf8))
        XCTAssertFalse(record.automatic)
    }
}

final class HighlightRoutingTests: XCTestCase {
    func testWhichRequestsAreHighlights() {
        func route(_ method: String, _ target: String) -> HighlightEngine.Route? {
            HighlightEngine.route(GatewayRequest(method: method, target: target))
        }
        XCTAssertEqual(route("GET", "/api/artifacts/a%20b/highlights"), .list(slug: "a b"))
        XCTAssertEqual(route("POST", "/api/artifacts/x/highlights"), .create(slug: "x"))
        XCTAssertEqual(route("PATCH", "/api/artifacts/x/highlights/abc"), .update(slug: "x", id: "abc"))
        XCTAssertEqual(route("DELETE", "/api/artifacts/x/highlights/abc"), .delete(slug: "x", id: "abc"))
        XCTAssertNil(route("PUT", "/api/artifacts/x/highlights/abc"))
        XCTAssertNil(route("GET", "/api/artifacts/x/blocks"))
        XCTAssertNil(route("GET", "/api/offline/artifacts/x/highlights"))
    }

    private func bundle(_ harness: OfflineHarness) async throws {
        try await harness.save(try ReviewFixture.pageBundle(try ReviewFixture.json()["highlights"]["bundle"], highlights: []))
    }

    private let create = #"{"block_key": "b01", "quote": "Some text", "prefix": "", "suffix": "", "start": 20, "end": 29}"#

    func testWithChangesWaitingTheyAreUploadedBeforeTheServerIsAsked() async throws {
        let harness = OfflineHarness()
        try await bundle(harness)
        harness.offline()
        let made = await harness.gateway.reply(to: GatewayRequest(method: "POST", target: "/api/artifacts/hl/highlights",
                                                                  body: Data(create.utf8)))
        XCTAssertEqual(made.status, 201)
        let highlight = try await made.json()
        // Back online: the change goes first, then the page's GET reaches the server.
        let serverList = JSONValue.object(["artifact": "hl", "highlights": .array([highlight])])
        let upload = JSONValue.object(["artifact": "hl", "applied": 1, "unchanged": 0, "stale": 0, "deleted": 0, "rejected": 0,
                                       "results": .array([.object(["id": highlight["id"], "op": "create", "status": "applied",
                                                                   "reason": .null, "detail": .null, "highlight": highlight])]),
                                       "highlights": .array([highlight])])
        harness.transport.setHandler { request, _ in
            if request.url?.path() == "/api/offline/artifacts/hl/highlights" {
                return FakeTransport.answer(200, ["Content-Type": "application/json"], data: upload.data())
            }
            return FakeTransport.answer(200, ["Content-Type": "application/json"], data: serverList.data())
        }
        let list = await harness.get("/api/artifacts/hl/highlights")
        XCTAssertEqual(list.source, .server)
        // (The first request is the one tried while offline.)
        XCTAssertEqual(harness.transport.requests.map { $0.url?.path() ?? "" },
                       ["/api/artifacts/hl/highlights", "/api/offline/artifacts/hl/highlights", "/api/artifacts/hl/highlights"])
        let page = await harness.page("hl")
        XCTAssertEqual(page?.highlights.changes, [])
        XCTAssertEqual(page?.highlights.list, [highlight])
    }

    func testWhenTheUploadFailsThePhoneAnswersAndTheChangesWait() async throws {
        let harness = OfflineHarness()
        try await bundle(harness)
        harness.offline()
        _ = await harness.gateway.reply(to: GatewayRequest(method: "POST", target: "/api/artifacts/hl/highlights", body: Data(create.utf8)))
        harness.transport.setHandler { _, _ in FakeTransport.answer(500, [:], body: #"{"detail":"database is locked"}"#) }
        let list = await harness.get("/api/artifacts/hl/highlights")
        XCTAssertEqual(list.source, .local("saved-page"))
        let shown = try await list.json()
        XCTAssertEqual(shown["highlights"].array?.count, 1, "the phone's list, with the change in it")
        let page = await harness.page("hl")
        XCTAssertEqual(page?.highlights.changes.count, 1)
        let log = await harness.store.log(harness.namespace)
        XCTAssertTrue(log.first?.message.contains("database is locked") == true)
    }

    func testRefreshingASavedPageKeepsHighlightChangesThatWait() async throws {
        let harness = OfflineHarness()
        try await bundle(harness)
        harness.offline()
        _ = await harness.gateway.reply(to: GatewayRequest(method: "POST", target: "/api/artifacts/hl/highlights", body: Data(create.utf8)))
        // A bundle from the server (with none of the phone's highlights) does not replace them.
        let fresh = try ReviewFixture.pageBundle(try ReviewFixture.json()["highlights"]["bundle"], highlights: [])
        try await harness.store.noteServerBundle(harness.namespace, fresh)
        let page = await harness.page("hl")
        XCTAssertEqual(page?.highlights.list?.count, 1)
        XCTAssertEqual(page?.highlights.changes.count, 1)
    }

    func testAPageSavedWithoutHighlightsSaysSoOffline() async throws {
        let harness = OfflineHarness()
        try await harness.save(try OfflineFixture.studyBundle()) // an older server: no "highlights" in the bundle
        harness.offline()
        let list = await harness.get("/api/artifacts/study/highlights")
        XCTAssertEqual(list.status, 503)
        let body = try await list.collectBody()
        XCTAssertTrue(detail(of: body)?.contains("saved without its highlights") == true)
    }
}

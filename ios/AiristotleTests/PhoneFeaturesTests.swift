import XCTest
@testable import Airistotle

private let sessionJSON = """
{"token":"tok-123","expires_at":"2026-12-25T10:00:00.123456+00:00",\
"user":{"id":7,"email":"reader@example.test","created_at":"2026-11-25T10:00:00+00:00"}}
"""

private func freshDefaults(_ name: String = #function) -> UserDefaults {
    UserDefaults(suiteName: "PhoneFeaturesTests-\(name)-\(UUID().uuidString)")! // swiftlint:disable:this force_unwrapping
}

// MARK: - Reopen where you left off

final class LastPageStoreTests: XCTestCase {
    func testWhichTargetsAreRemembered() {
        XCTAssertTrue(LastPage.isRememberable("/"))
        XCTAssertTrue(LastPage.isRememberable("/artifacts/attention"))
        XCTAssertTrue(LastPage.isRememberable("/library?q=attention%20is"))
        XCTAssertFalse(LastPage.isRememberable("/login?next=/library"))
        XCTAssertFalse(LastPage.isRememberable("/register"))
        XCTAssertFalse(LastPage.isRememberable("/logout"))
        XCTAssertFalse(LastPage.isRememberable("/api/history/export"))
        XCTAssertFalse(LastPage.isRememberable("/static/css/app.css"))
        XCTAssertFalse(LastPage.isRememberable("//elsewhere.test/"))
        XCTAssertFalse(LastPage.isRememberable("library"))
    }

    func testAPageAndItsScrollAreKeptPerNamespace() {
        let store = LastPageStore(defaults: freshDefaults())
        store.remember(target: "/artifacts/a", for: "one")
        store.remember(scrollY: 812.5, of: "/artifacts/a", for: "one")
        store.remember(target: "/library", for: "two")
        XCTAssertEqual(store.page(for: "one"), LastPage(target: "/artifacts/a", scrollY: 812.5))
        XCTAssertEqual(store.page(for: "two"), LastPage(target: "/library", scrollY: 0))
        XCTAssertNil(store.page(for: "three"))

        // The same page again (a reload, the launch load settling) keeps its position ...
        store.remember(target: "/artifacts/a", for: "one")
        XCTAssertEqual(store.page(for: "one")?.scrollY, 812.5)
        // ... a late report from a page that was left is ignored, a new page starts at the top.
        store.remember(target: "/graph", for: "one")
        store.remember(scrollY: 300, of: "/artifacts/a", for: "one")
        XCTAssertEqual(store.page(for: "one"), LastPage(target: "/graph", scrollY: 0))
        // Sign-in pages are never remembered.
        store.remember(target: "/login?next=/graph", for: "one")
        XCTAssertEqual(store.page(for: "one")?.target, "/graph")

        store.forget("one")
        XCTAssertNil(store.page(for: "one"))
        XCTAssertNotNil(store.page(for: "two"))
        store.forgetAll()
        XCTAssertNil(store.page(for: "two"))
    }

    func testAnUnreadableValueIsRemovedNotUsed() {
        let defaults = freshDefaults()
        defaults.set(Data("not json".utf8), forKey: LastPageStore.keyPrefix + "one")
        let store = LastPageStore(defaults: defaults)
        XCTAssertNil(store.page(for: "one"))
        XCTAssertNil(defaults.data(forKey: LastPageStore.keyPrefix + "one"))
    }
}

@MainActor
final class LastPageAppModelTests: XCTestCase {
    private func makeModel(_ transport: FakeTransport, store: EphemeralSessionStore, defaults: UserDefaults) -> AppModel {
        AppModel(transport: transport, store: store, cache: ResponseCache(root: TestFiles.folder()), defaults: defaults)
    }

    private static let account = StoredSession(server: .test, kind: .account(
        token: "tok-123", user: AccountUser(id: 7, email: "reader@example.test", createdAt: "x"), expiresAt: "y"
    ))

    func testALaunchReopensTheLastPageAtItsPosition() async throws {
        let defaults = freshDefaults()
        let store = EphemeralSessionStore()
        try store.save(Self.account)
        LastPageStore(defaults: defaults).remember(target: "/artifacts/a", for: Self.account.connection.cacheNamespace)
        LastPageStore(defaults: defaults).remember(scrollY: 640, of: "/artifacts/a", for: Self.account.connection.cacheNamespace)
        let model = makeModel(FakeTransport { _, _ in FakeTransport.answer(200, [:], body: sessionJSON) }, store: store, defaults: defaults)
        await model.restore()
        XCTAssertEqual(model.startTarget, "/artifacts/a")
        XCTAssertEqual(model.startScroll, 640)
    }

    func testWithoutALastPageALaunchStartsAtHome() async throws {
        let store = EphemeralSessionStore()
        try store.save(Self.account)
        let model = makeModel(FakeTransport { _, _ in FakeTransport.answer(200, [:], body: sessionJSON) }, store: store,
                              defaults: freshDefaults())
        await model.restore()
        XCTAssertEqual(model.startTarget, "/")
        XCTAssertNil(model.startScroll)
    }

    func testTheModelRemembersThePageOnScreenAndSigningOutForgetsIt() async throws {
        let defaults = freshDefaults()
        let transport = FakeTransport { _, _ in FakeTransport.answer(200, [:], body: sessionJSON) }
        let model = makeModel(transport, store: EphemeralSessionStore(), defaults: defaults)
        model.rememberPage("/library") // signed out: nothing is kept
        try await model.signIn(server: "https://example.test", email: "reader@example.test", password: "pw")
        let namespace = try XCTUnwrap(model.gateway.connection?.cacheNamespace)
        XCTAssertNil(model.lastPages.page(for: namespace))
        model.rememberPage("/artifacts/b")
        model.rememberScroll("/artifacts/b", scrollY: 99)
        XCTAssertEqual(model.lastPages.page(for: namespace), LastPage(target: "/artifacts/b", scrollY: 99))

        transport.setHandler { _, _ in FakeTransport.answer(204) }
        await model.signOut()
        XCTAssertNil(model.lastPages.page(for: namespace), "signing out forgets it")

        transport.setHandler { _, _ in FakeTransport.answer(200, [:], body: sessionJSON) }
        try await model.signIn(server: "https://example.test", email: "reader@example.test", password: "pw")
        XCTAssertEqual(model.startTarget, "/")
    }

    func testASignInAfterA401GoesWhereTheServerInterrupted() async throws {
        let defaults = freshDefaults()
        let model = makeModel(FakeTransport { _, _ in FakeTransport.answer(200, [:], body: sessionJSON) },
                              store: EphemeralSessionStore(), defaults: defaults)
        try await model.signIn(server: "https://example.test", email: "reader@example.test", password: "pw")
        model.rememberPage("/graph")
        model.rememberScroll("/graph", scrollY: 120)

        model.handle(.sessionRejected(returnTo: "/artifacts/s"))
        try await model.signIn(server: "https://example.test", email: "reader@example.test", password: "pw")
        XCTAssertEqual(model.startTarget, "/artifacts/s", "the interrupted page, not the remembered one")
        XCTAssertNil(model.startScroll)

        // A 401 with nothing to return to: the remembered page (a session rejection is not a sign-out).
        model.handle(.sessionRejected(returnTo: nil))
        try await model.signIn(server: "https://example.test", email: "reader@example.test", password: "pw")
        XCTAssertEqual(model.startTarget, "/graph")
        XCTAssertEqual(model.startScroll, 120)
    }
}

// MARK: - Haptics for graded answers

final class GradedAnswerTests: XCTestCase {
    private func post(_ target: String, method: String = "POST") -> GatewayRequest {
        GatewayRequest(method: method, target: target, headers: ["Content-Type": "application/json"], body: Data("{}".utf8))
    }

    func testWhichRequestsGradeAnAnswer() {
        XCTAssertTrue(GradedAnswer.isSubmission(post("/api/artifacts/attention/blocks/b02/responses")))
        XCTAssertTrue(GradedAnswer.isSubmission(post("/api/artifacts/attention/chat/blocks/41/responses")))
        XCTAssertTrue(GradedAnswer.isSubmission(post("/api/artifacts/a%20b/blocks/b02/responses?x=1")))
        XCTAssertFalse(GradedAnswer.isSubmission(post("/api/artifacts/attention/blocks/b02/responses", method: "GET")))
        XCTAssertFalse(GradedAnswer.isSubmission(post("/api/artifacts/attention/blocks")))
        XCTAssertFalse(GradedAnswer.isSubmission(post("/api/artifacts/attention/blocks//responses")))
        XCTAssertFalse(GradedAnswer.isSubmission(post("/api/offline/artifacts/attention/attempts")))
        XCTAssertFalse(GradedAnswer.isSubmission(post("/api/artifacts/attention/chat")))
        XCTAssertFalse(GradedAnswer.isSubmission(post("/api/artifacts/attention/other/blocks/1/responses")))
    }

    func testTheReplyDecidesTheOutcome() {
        func outcome(_ body: String, status: Int = 201) -> AnswerOutcome? {
            GradedAnswer.outcome(status: status, body: Data(body.utf8))
        }
        XCTAssertEqual(outcome(#"{"attempt":1,"passed":true,"verdict":"passed","state":"passed"}"#), .passed)
        XCTAssertEqual(outcome(#"{"passed":false,"verdict":"failed"}"#), .failed)
        XCTAssertEqual(outcome(#"{"passed":false,"verdict":"partial","missing":["x"]}"#), .partial)
        // Not classified: no haptic rather than a wrong one.
        XCTAssertNil(outcome(#"{"passed":true,"verdict":"passed"}"#, status: 200))
        XCTAssertNil(outcome(#"{"detail":"block b02 is already passed"}"#, status: 409))
        XCTAssertNil(outcome(#"{"passed":true,"verdict":"failed"}"#), "the two fields disagree")
        XCTAssertNil(outcome(#"{"passed":false,"verdict":"passed"}"#), "the two fields disagree")
        XCTAssertNil(outcome(#"{"passed":1,"verdict":"passed"}"#), "passed must be a boolean")
        XCTAssertNil(outcome(#"{"passed":true}"#))
        XCTAssertNil(outcome(#"{"verdict":"passed"}"#))
        XCTAssertNil(outcome(#"{"passed":true,"verdict":"excellent"}"#))
        XCTAssertNil(outcome(#"[{"passed":true,"verdict":"passed"}]"#))
        XCTAssertNil(outcome("not json"))
        XCTAssertNil(outcome(""))
    }

    func testTheServersReplyIsPassedOnAndItsGradeReported() async throws {
        let body = #"{"attempt":2,"passed":true,"verdict":"passed","feedback":"Yes."}"#
        let transport = FakeTransport { _, _ in FakeTransport.answer(201, ["Content-Type": "application/json"], body: body) }
        let recorder = EventRecorder()
        let gateway = Gateway(transport: transport, cache: ResponseCache(root: TestFiles.folder()), events: recorder.sink)
        gateway.connection = .test
        let reply = await gateway.reply(to: post("/api/artifacts/s/blocks/b02/responses"))
        XCTAssertFalse(recorder.events.contains(.answerGraded(.passed)), "only once the page has the whole reply")
        let received = try await reply.collectBody()
        XCTAssertEqual(received.text, body)
        XCTAssertEqual(recorder.events.filter { if case .answerGraded = $0 { true } else { false } }, [.answerGraded(.passed)])

        // Other requests are not read for a grade.
        _ = try await gateway.reply(to: post("/api/artifacts/s/chat")).collectBody()
        XCTAssertEqual(recorder.events.filter { if case .answerGraded = $0 { true } else { false } }.count, 1)
    }

    func testAnAnswerGradedOnThePhoneIsReported() async throws {
        let harness = OfflineHarness()
        try await harness.save(try OfflineFixture.studyBundle())
        harness.offline()
        _ = try await harness.submit("b02", #"{"response": {"choice": 0}}"#).collectBody()
        _ = try await harness.submit("b02", #"{"response": {"choice": 1}}"#).collectBody()
        let malformed = await harness.submit("b02", #"{"response": {"choice": 7}}"#)
        _ = try await malformed.collectBody()
        let grades = harness.recorder.events.compactMap { event -> AnswerOutcome? in
            if case .answerGraded(let outcome) = event { outcome } else { nil }
        }
        XCTAssertEqual(grades, [.failed, .passed], "a refused answer (422) gives nothing")
    }

    @MainActor
    func testTheModelKeepsEachGradeForTheHaptic() {
        let model = AppModel(transport: FakeTransport(), store: EphemeralSessionStore(),
                             cache: ResponseCache(root: TestFiles.folder()), defaults: freshDefaults())
        XCTAssertNil(model.lastGrade)
        model.handle(.answerGraded(.passed))
        let first = model.lastGrade
        XCTAssertEqual(first?.outcome, .passed)
        model.handle(.answerGraded(.passed))
        XCTAssertNotEqual(model.lastGrade, first, "the same outcome twice is two haptics")
    }
}

// MARK: - Text size and the screen

@MainActor
final class ReaderSettingsTests: XCTestCase {
    func testTextSizeDefaultsStepsAndPersists() {
        let defaults = freshDefaults()
        let settings = ReaderSettings(defaults: defaults)
        XCTAssertEqual(settings.textSizePercent, 100)
        XCTAssertEqual(TextSize.range, 85...150)
        settings.setTextSize(TextSize.larger(than: settings.textSizePercent) ?? 0)
        XCTAssertEqual(settings.textSizePercent, 110)
        settings.setTextSize(150)
        XCTAssertNil(TextSize.larger(than: 150), "150% is the largest")
        XCTAssertEqual(ReaderSettings(defaults: defaults).textSizePercent, 150, "kept across launches")
        settings.setTextSize(85)
        XCTAssertNil(TextSize.smaller(than: 85), "85% is the smallest")
        XCTAssertEqual(TextSize.smaller(than: 110), 100)
        settings.setTextSize(400)
        XCTAssertEqual(settings.textSizePercent, 150, "brought onto the scale")
        defaults.set(102, forKey: TextSize.key)
        XCTAssertEqual(ReaderSettings(defaults: defaults).textSizePercent, 100)
        XCTAssertEqual(TextSize.css(130), "html { font-size: 130% !important; }")
        XCTAssertTrue(PageShims.appStyle(textSizePercent: 130).contains(TextSize.css(130)))
        XCTAssertTrue(PageShims.appStyle(textSizePercent: 130).contains(".nav-signout { display: none !important; }"))
    }

    func testKeepingTheScreenAwakeIsOnByDefaultAndKept() {
        let defaults = freshDefaults()
        let settings = ReaderSettings(defaults: defaults)
        XCTAssertTrue(settings.keepScreenAwake)
        settings.keepScreenAwake = false
        XCTAssertFalse(ReaderSettings(defaults: defaults).keepScreenAwake)
    }

    func testOnlyStudyPagesCount() {
        XCTAssertTrue(ScreenAwakePolicy.isStudyPage("/artifacts/attention"))
        XCTAssertTrue(ScreenAwakePolicy.isStudyPage("/artifacts/attention?full=1"))
        XCTAssertTrue(ScreenAwakePolicy.isStudyPage("/artifacts/a%20b#b04"))
        XCTAssertFalse(ScreenAwakePolicy.isStudyPage("/artifacts"))
        XCTAssertFalse(ScreenAwakePolicy.isStudyPage("/artifacts/"))
        XCTAssertFalse(ScreenAwakePolicy.isStudyPage("/artifacts/attention/blocks"))
        XCTAssertFalse(ScreenAwakePolicy.isStudyPage("/labs/attention-lab"))
        XCTAssertFalse(ScreenAwakePolicy.isStudyPage("/library"))
        XCTAssertFalse(ScreenAwakePolicy.isStudyPage("/"))
        XCTAssertFalse(ScreenAwakePolicy.isStudyPage(nil))
    }

    func testTheScreenStaysOnOnlyWhenEverythingHolds() {
        let study = "/artifacts/attention"
        XCTAssertTrue(ScreenAwakePolicy.keepAwake(enabled: true, target: study, appActive: true, signedIn: true))
        XCTAssertFalse(ScreenAwakePolicy.keepAwake(enabled: false, target: study, appActive: true, signedIn: true))
        XCTAssertFalse(ScreenAwakePolicy.keepAwake(enabled: true, target: study, appActive: false, signedIn: true))
        XCTAssertFalse(ScreenAwakePolicy.keepAwake(enabled: true, target: study, appActive: true, signedIn: false))
        XCTAssertFalse(ScreenAwakePolicy.keepAwake(enabled: true, target: "/library", appActive: true, signedIn: true))
    }
}

// MARK: - The bottom bar

final class BarVisibilityTests: XCTestCase {
    private var bar = BarVisibility()

    private func scroll(_ offsets: [CGFloat], byReader: Bool = true, content: CGFloat = 5000, viewport: CGFloat = 700) {
        for offset in offsets {
            bar.update(.init(offset: offset, contentHeight: content, viewportHeight: viewport, byReader: byReader))
        }
    }

    func testHidesOnScrollingDownAndShowsOnScrollingUp() {
        scroll([500], byReader: false)
        scroll([510, 520])
        XCTAssertTrue(bar.isVisible, "20 points down is not enough")
        scroll([530])
        XCTAssertFalse(bar.isVisible)
        scroll([520, 510])
        XCTAssertFalse(bar.isVisible, "a small move back does not bring it")
        scroll([505])
        XCTAssertTrue(bar.isVisible, "25 points up from where it turned")
        scroll([515, 525])
        XCTAssertTrue(bar.isVisible, "20 points down from where it turned")
        scroll([531])
        XCTAssertFalse(bar.isVisible)
    }

    func testShowsNearTheTopAndAtTheEnd() {
        scroll([500], byReader: false)
        scroll([600])
        XCTAssertFalse(bar.isVisible)
        scroll([30])
        XCTAssertTrue(bar.isVisible, "near the top")
        scroll([200, 4000])
        XCTAssertFalse(bar.isVisible)
        scroll([4299])
        XCTAssertTrue(bar.isVisible, "at the end of the page (5000 - 700)")
    }

    func testRubberBandingChangesNothing() {
        scroll([500], byReader: false)
        scroll([600])
        XCTAssertFalse(bar.isVisible)
        scroll([4350, 4400, 4320])
        XCTAssertFalse(bar.isVisible, "bouncing past the end")
        scroll([4300])
        XCTAssertTrue(bar.isVisible, "at the end")
        scroll([4380, 4340, 4310, 4300])
        XCTAssertTrue(bar.isVisible, "bouncing at the end, no flicker")
        scroll([-80, -40, 0])
        XCTAssertTrue(bar.isVisible, "pulled down past the top")
    }

    func testScrollingTheReaderDidNotDoIsIgnored() {
        scroll([0, 2000], byReader: false) // a restored position
        XCTAssertTrue(bar.isVisible)
        scroll([2010], byReader: true)
        XCTAssertTrue(bar.isVisible, "the run starts where the page was put")
        scroll([2040], byReader: true)
        XCTAssertFalse(bar.isVisible)
    }

    func testAShortPageAlwaysShowsIt() {
        scroll([0, 30, 60], content: 760, viewport: 700)
        XCTAssertTrue(bar.isVisible)
    }

    func testANewPageShowsIt() {
        scroll([0, 200, 400])
        XCTAssertFalse(bar.isVisible)
        bar.reset()
        XCTAssertTrue(bar.isVisible)
    }
}

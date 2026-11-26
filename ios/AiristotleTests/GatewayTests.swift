import XCTest
@testable import Airistotle

final class GatewayTests: XCTestCase {
    private var transport: FakeTransport!
    private var cache: ResponseCache!
    private var recorder: EventRecorder!
    private var gateway: Gateway!

    override func setUp() {
        transport = FakeTransport()
        cache = ResponseCache(root: TestFiles.folder())
        recorder = EventRecorder()
        gateway = Gateway(transport: transport, cache: cache, events: recorder.sink)
        gateway.connection = .test
    }

    private func page(_ target: String) -> GatewayRequest {
        GatewayRequest(method: "GET", target: target, headers: ["Accept": "text/html"], isNavigation: true, isMainFrame: true)
    }

    private func api(_ method: String, _ target: String, body: Data? = nil) -> GatewayRequest {
        GatewayRequest(method: method, target: target, headers: ["Accept": "application/json"], body: body)
    }

    private func goOffline(_ code: URLError.Code = .cannotConnectToHost) {
        transport.setHandler { _, _ in throw URLError(code) }
    }

    // MARK: Online

    func testForwardsAndPassesTheAnswerBack() async throws {
        transport.setHandler { request, _ in
            XCTAssertEqual(request.url?.absoluteString, "https://example.test/api/artifacts/s?full=1")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer test-token")
            return FakeTransport.answer(200, [
                "Content-Type": "application/json", "Content-Encoding": "gzip", "Content-Length": "99",
                "Set-Cookie": "x=y", "ETag": "\"1\""
            ], body: "{\"slug\":\"s\"}")
        }
        let reply = await gateway.reply(to: api("GET", "/api/artifacts/s?full=1"))
        XCTAssertEqual(reply.status, 200)
        XCTAssertEqual(reply.source, .server)
        XCTAssertEqual(reply.header("Content-Type"), "application/json")
        XCTAssertEqual(reply.header("ETag"), "\"1\"")
        XCTAssertNil(reply.header("Content-Encoding"), "URLSession already decoded the body")
        XCTAssertNil(reply.header("Content-Length"))
        XCTAssertNil(reply.header("Set-Cookie"))
        let body = try await reply.collectBody()
        XCTAssertEqual(body.text, "{\"slug\":\"s\"}")
        XCTAssertEqual(recorder.events, [.serverReachable])
    }

    func testJavaScriptKeepsItsMIMEType() async throws {
        transport.setHandler { _, _ in FakeTransport.answer(200, ["Content-Type": "text/javascript; charset=utf-8"], body: "export {}") }
        let reply = await gateway.reply(to: api("GET", "/static/js/home.js"))
        XCTAssertEqual(reply.header("Content-Type"), "text/javascript; charset=utf-8")
    }

    func testServerErrorsPassThroughUnchanged() async throws {
        transport.setHandler { _, _ in FakeTransport.answer(409, ["Content-Type": "application/json"], body: "{\"detail\":\"taken\"}") }
        let reply = await gateway.reply(to: api("POST", "/api/artifacts", body: Data("{}".utf8)))
        XCTAssertEqual(reply.status, 409)
        let body = try await reply.collectBody()
        XCTAssertEqual(detail(of: body), "taken")
        XCTAssertEqual(reply.source, .server)
    }

    func testStreamedBodiesArriveChunkByChunk() async throws {
        let (body, feed) = AsyncThrowingStream<Data, any Error>.makeStream()
        transport.setHandler { _, _ in
            let head = HTTPURLResponse(url: URL(string: "https://example.test/")!, statusCode: 200, httpVersion: nil,
                                       headerFields: ["Content-Type": "text/event-stream"])!
            return TransportResponse(response: head, body: body)
        }
        let reply = await gateway.reply(to: api("POST", "/api/artifacts/s/chat/threads/1/messages", body: Data("{}".utf8)))
        var iterator = reply.body.makeAsyncIterator()
        feed.yield(Data("event: delta\ndata: {\"text\":\"Hel\"}\n\n".utf8))
        let first = try await iterator.next()
        XCTAssertEqual(first?.text, "event: delta\ndata: {\"text\":\"Hel\"}\n\n", "the first piece arrives before the rest exists")
        feed.yield(Data("event: done\ndata: {}\n\n".utf8))
        feed.finish()
        let second = try await iterator.next()
        XCTAssertEqual(second?.text, "event: done\ndata: {}\n\n")
        let end = try await iterator.next()
        XCTAssertNil(end)
        XCTAssertNil(cache.lookup(namespace: GatewayConnection.test.cacheNamespace, target: "/api/artifacts/s/chat/threads/1/messages"))
    }

    func testCachedGetsStillStreamWhileSaving() async throws {
        let (body, feed) = AsyncThrowingStream<Data, any Error>.makeStream()
        transport.setHandler { _, _ in
            let head = HTTPURLResponse(url: URL(string: "https://example.test/")!, statusCode: 200, httpVersion: nil,
                                       headerFields: ["Content-Type": "text/html"])!
            return TransportResponse(response: head, body: body)
        }
        let reply = await gateway.reply(to: page("/library"))
        var iterator = reply.body.makeAsyncIterator()
        feed.yield(Data("one".utf8))
        let first = try await iterator.next()
        XCTAssertEqual(first?.text, "one")
        XCTAssertNil(cache.lookup(namespace: GatewayConnection.test.cacheNamespace, target: "/library"), "saved only when complete")
        feed.yield(Data("two".utf8))
        feed.finish()
        while try await iterator.next() != nil {}
        let saved = try XCTUnwrap(cache.lookup(namespace: GatewayConnection.test.cacheNamespace, target: "/library"))
        XCTAssertEqual(try Data(contentsOf: saved.bodyFile).text, "onetwo")
    }

    func testABrokenBodyIsNotSaved() async throws {
        let (body, feed) = AsyncThrowingStream<Data, any Error>.makeStream()
        transport.setHandler { _, _ in
            let head = HTTPURLResponse(url: URL(string: "https://example.test/")!, statusCode: 200, httpVersion: nil, headerFields: [:])!
            return TransportResponse(response: head, body: body)
        }
        let reply = await gateway.reply(to: api("GET", "/api/library"))
        feed.yield(Data("half".utf8))
        feed.finish(throwing: URLError(.networkConnectionLost))
        do {
            _ = try await reply.collectBody()
            XCTFail("the broken body must fail")
        } catch {
            XCTAssertEqual((error as? URLError)?.code, .networkConnectionLost)
        }
        XCTAssertNil(cache.lookup(namespace: GatewayConnection.test.cacheNamespace, target: "/api/library"))
    }

    func testWhatIsSaved() async throws {
        // swiftlint:disable:next large_tuple
        let cases: [(GatewayRequest, Int, [String: String], Bool)] = [
            (api("GET", "/api/library"), 200, ["Content-Type": "application/json"], true),
            (api("GET", "/static/css/app.css"), 200, ["Cache-Control": "no-cache"], true),
            (api("POST", "/api/library"), 200, [:], false),
            (api("GET", "/api/missing"), 404, [:], false),
            (api("GET", "/api/private"), 200, ["Cache-Control": "no-store"], false),
            (api("GET", "/api/stream"), 200, ["Content-Type": "text/event-stream"], false),
            (api("GET", "/api/auth/me"), 200, [:], false),
            (api("HEAD", "/api/library"), 200, [:], false)
        ]
        for (request, status, headers, expected) in cases {
            XCTAssertEqual(gateway.shouldCache(request, status: status, headers: headers), expected, "\(request.method) \(request.target)")
        }
    }

    // MARK: Offline

    func testOfflineGetIsAnsweredFromTheSavedCopy() async throws {
        transport.setHandler { _, _ in FakeTransport.answer(200, ["Content-Type": "text/html"], body: "<h1>Library</h1>") }
        _ = try await gateway.reply(to: page("/library")).collectBody()

        goOffline()
        let reply = await gateway.reply(to: page("/library"))
        XCTAssertEqual(reply.status, 200)
        let body = try await reply.collectBody()
        XCTAssertEqual(body.text, "<h1>Library</h1>")
        XCTAssertEqual(reply.header("Content-Type"), "text/html")
        XCTAssertEqual(reply.header(Gateway.sourceHeader), "cache")
        XCTAssertNotNil(reply.header(Gateway.storedAtHeader))
        guard case .cache = reply.source else { return XCTFail("expected a saved copy, got \(reply.source)") }
        XCTAssertTrue(recorder.events.contains(.serverUnreachable(URLError(.cannotConnectToHost).localizedDescription)))
        XCTAssertTrue(recorder.events.contains { if case .pageShown("/library", .cache) = $0 { true } else { false } })
    }

    func testOfflineGetWithoutACopyIsAnExplicitError() async throws {
        goOffline(.notConnectedToInternet)
        let reply = await gateway.reply(to: api("GET", "/api/library"))
        XCTAssertEqual(reply.status, 503)
        XCTAssertEqual(reply.source, .gatewayError)
        XCTAssertEqual(reply.header("Content-Type"), "application/json")
        let body = try await reply.collectBody()
        let message = try XCTUnwrap(detail(of: body))
        XCTAssertTrue(message.hasPrefix("The phone is offline: example.test could not be reached"), message)
        XCTAssertTrue(message.contains("not been saved on this phone"), message)
    }

    func testOfflinePageWithoutACopyIsAnHTMLPage() async throws {
        goOffline()
        let reply = await gateway.reply(to: page("/graph"))
        XCTAssertEqual(reply.status, 503)
        XCTAssertEqual(reply.header("Content-Type"), "text/html; charset=utf-8")
        let html = try await reply.collectBody().text
        XCTAssertTrue(html.contains("<title>Offline</title>"))
        XCTAssertTrue(html.contains("The phone is offline"))
        XCTAssertTrue(html.contains("href=\"/graph\""), "a Try again link")
    }

    func testOfflineChangesAreRefusedEvenWhenAGetIsSaved() async throws {
        transport.setHandler { _, _ in FakeTransport.answer(200, [:], body: "{}") }
        _ = try await gateway.reply(to: api("GET", "/api/artifacts/s/responses")).collectBody()
        goOffline()
        for method in ["POST", "PUT", "PATCH", "DELETE"] {
            let reply = await gateway.reply(to: api(method, "/api/artifacts/s/responses", body: Data("{}".utf8)))
            XCTAssertEqual(reply.status, 503, method)
            let body = try await reply.collectBody()
            let message = try XCTUnwrap(detail(of: body))
            XCTAssertTrue(message.contains("This was not sent to the server and nothing was saved."), message)
        }
    }

    func testOtherTransportFailuresAreNotCalledOffline() async throws {
        transport.setHandler { _, _ in throw URLError(.serverCertificateUntrusted) }
        let reply = await gateway.reply(to: api("GET", "/api/library"))
        XCTAssertEqual(reply.status, 502)
        let body = try await reply.collectBody()
        let message = try XCTUnwrap(detail(of: body))
        XCTAssertTrue(message.contains("failed before the server answered"), message)
        XCTAssertFalse(recorder.events.contains { if case .serverUnreachable = $0 { true } else { false } })
    }

    func testCopiesBelongToTheirAccount() async throws {
        transport.setHandler { _, _ in FakeTransport.answer(200, [:], body: "mine") }
        _ = try await gateway.reply(to: api("GET", "/api/library")).collectBody()
        gateway.connection = GatewayConnection(server: .test, token: "other", account: "user-2")
        goOffline()
        let reply = await gateway.reply(to: api("GET", "/api/library"))
        XCTAssertEqual(reply.status, 503, "another account's copy is never shown")
    }

    // MARK: Sessions

    func testA401AsksToSignInAgainAndReachesThePage() async throws {
        transport.setHandler { _, _ in
            FakeTransport.answer(401, ["Content-Type": "application/json"], body: "{\"detail\":\"Sign in first.\"}")
        }
        let reply = await gateway.reply(to: api("GET", "/api/library"))
        XCTAssertEqual(reply.status, 401)
        let body = try await reply.collectBody()
        XCTAssertEqual(detail(of: body), "Sign in first.")
        XCTAssertTrue(recorder.events.contains(.sessionRejected(returnTo: nil)))
    }

    func testARedirectToTheSignInPageIsNeverShown() async throws {
        transport.setHandler { _, follow in
            XCTAssertFalse(follow, "a navigation's redirect is handled by the gateway")
            return FakeTransport.answer(303, ["Location": "/login?next=%2Fartifacts%2Fs"])
        }
        let reply = await gateway.reply(to: page("/artifacts/s"))
        XCTAssertEqual(reply.status, 401)
        let html = try await reply.collectBody().text
        XCTAssertTrue(html.contains("Sign in again"))
        XCTAssertTrue(recorder.events.contains(.sessionRejected(returnTo: "/artifacts/s")))
    }

    func testANavigationRedirectBecomesAPageThatMovesOn() async throws {
        transport.setHandler { _, _ in FakeTransport.answer(307, ["Location": "/labs/my-lab"]) }
        let reply = await gateway.reply(to: page("/artifacts/my-lab"))
        XCTAssertEqual(reply.status, 200)
        let html = try await reply.collectBody().text
        XCTAssertTrue(html.contains("location.replace(\"teacher://app/labs/my-lab\")"), html)
    }

    func testScriptRequestsFollowRedirectsInTheTransport() async throws {
        transport.setHandler { _, _ in FakeTransport.answer(200, [:], body: "{}") }
        _ = await gateway.reply(to: api("GET", "/api/library"))
        _ = await gateway.reply(to: page("/library"))
        XCTAssertEqual(transport.followFlags, [true, false])
    }

    func testARedirectAwayIsShownNotFollowed() async throws {
        transport.setHandler { _, _ in FakeTransport.answer(302, ["Location": "https://elsewhere.test/x"]) }
        let reply = await gateway.reply(to: page("/x"))
        XCTAssertEqual(reply.status, 502)
        let body = try await reply.collectBody()
        XCTAssertTrue(body.text.contains("https://elsewhere.test/x"))
    }

    func testWithoutAConnectionNothingIsSent() async throws {
        gateway.connection = nil
        let reply = await gateway.reply(to: api("GET", "/api/library"))
        XCTAssertEqual(reply.status, 401)
        XCTAssertTrue(transport.requests.isEmpty)
        XCTAssertEqual(recorder.events, [.sessionRejected(returnTo: nil)])
    }

    // MARK: Local routes

    func testTheWebSignInPagesAreReplacedByTheApp() async throws {
        let reply = await gateway.reply(to: page("/login?next=%2Fgraph%3Fconcept%3Dattention"))
        XCTAssertEqual(reply.status, 401)
        XCTAssertEqual(reply.source, .local("auth-pages"))
        XCTAssertTrue(transport.requests.isEmpty, "the server's /login is never fetched")
        XCTAssertTrue(recorder.events.contains(.sessionRejected(returnTo: "/graph?concept=attention")))

        _ = await gateway.reply(to: page("/register"))
        XCTAssertTrue(transport.requests.isEmpty)
    }

    func testTheHeadersSignOutButtonSignsOutNatively() async throws {
        let reply = await gateway.reply(to: GatewayRequest(method: "POST", target: "/logout", isNavigation: true, isMainFrame: true))
        XCTAssertEqual(reply.status, 200)
        XCTAssertTrue(transport.requests.isEmpty)
        XCTAssertTrue(recorder.events.contains(.signOutRequested))
    }

    func testSafeReturnTargets() {
        XCTAssertEqual(AuthPagesRoute.safeReturn("/artifacts/s?x=1"), "/artifacts/s?x=1")
        XCTAssertNil(AuthPagesRoute.safeReturn("//evil.test/x"))
        XCTAssertNil(AuthPagesRoute.safeReturn("https://evil.test"))
        XCTAssertNil(AuthPagesRoute.safeReturn("/login?next=/x"))
        XCTAssertNil(AuthPagesRoute.safeReturn("/a\\b"))
        XCTAssertNil(AuthPagesRoute.safeReturn(nil))
    }

    func testRoutesArePluggable() async throws {
        struct Saved: GatewayRoute {
            let name = "saved"
            func reply(to request: GatewayRequest, context: GatewayContext) async -> GatewayReply? {
                request.path == "/saved" ? GatewayReply(status: 200, headers: [:], data: Data("local".utf8), source: .local(name)) : nil
            }
        }
        struct Grader: OfflineRoute {
            let name = "grader"
            func reply(to request: GatewayRequest, context: GatewayContext, failure: URLError) async -> GatewayReply? {
                request.method == "POST" ? .jsonError(202, "graded on the phone", source: .local(name)) : nil
            }
        }
        actor Seen: GatewayObserver {
            var statuses: [Int] = []
            func serverReplied(to request: GatewayRequest, status: Int, headers: [String: String], context: GatewayContext) async {
                statuses.append(status)
            }
        }
        let seen = Seen()
        let gateway = Gateway(transport: transport, cache: cache, routes: [Saved()], offlineRoutes: [Grader()],
                              observers: [seen], events: { _ in })
        gateway.connection = .test

        let local = await gateway.reply(to: api("GET", "/saved"))
        XCTAssertEqual(local.source, .local("saved"))
        XCTAssertTrue(transport.requests.isEmpty)

        transport.setHandler { _, _ in FakeTransport.answer(204) }
        _ = await gateway.reply(to: api("GET", "/api/x"))
        let statuses = await seen.statuses
        XCTAssertEqual(statuses, [204])

        goOffline()
        let graded = await gateway.reply(to: api("POST", "/api/answer"))
        XCTAssertEqual(graded.source, .local("grader"))
        let refused = await gateway.reply(to: api("PUT", "/api/answer"))
        XCTAssertEqual(refused.status, 503, "the offline route declined, so the gateway says it is offline")
    }
}

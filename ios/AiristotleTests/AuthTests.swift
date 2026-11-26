import XCTest
@testable import Airistotle

private let sessionJSON = """
{"token":"tok-123","expires_at":"2026-12-25T10:00:00.123456+00:00",\
"user":{"id":7,"email":"reader@example.test","created_at":"2026-11-25T10:00:00+00:00"}}
"""

final class AuthClientTests: XCTestCase {
    func testLoginSendsTheJSONAndReadsTheSession() async throws {
        let transport = FakeTransport { request, _ in
            XCTAssertEqual(request.url?.absoluteString, "https://example.test/api/auth/login")
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
            let body = try XCTUnwrap(request.httpBody.flatMap { try JSONSerialization.jsonObject(with: $0) as? [String: String] })
            XCTAssertEqual(body, ["email": "reader@example.test", "password": "pw", "client": AuthClient.clientLabel])
            return FakeTransport.answer(200, ["Content-Type": "application/json"], body: sessionJSON)
        }
        let session = try await AuthClient(transport: transport).login(server: .test, email: "reader@example.test", password: "pw")
        XCTAssertEqual(session.token, "tok-123")
        XCTAssertEqual(session.user, AccountUser(id: 7, email: "reader@example.test", createdAt: "2026-11-25T10:00:00+00:00"))
    }

    func testRegisterSendsTheAccessCode() async throws {
        let transport = FakeTransport { request, _ in
            XCTAssertEqual(request.url?.path(), "/api/auth/register")
            let body = try XCTUnwrap(request.httpBody.flatMap { try JSONSerialization.jsonObject(with: $0) as? [String: String] })
            XCTAssertEqual(body["access_code"], "code")
            return FakeTransport.answer(201, [:], body: sessionJSON)
        }
        let session = try await AuthClient(transport: transport)
            .register(server: .test, email: "reader@example.test", password: "pw", accessCode: "code")
        XCTAssertEqual(session.user.id, 7)
    }

    func testTheServersOwnMessageIsKept() async {
        let transport = FakeTransport { _, _ in
            FakeTransport.answer(401, [:], body: "{\"detail\":\"Wrong email or password. Try again.\"}")
        }
        do {
            _ = try await AuthClient(transport: transport).login(server: .test, email: "a@b.test", password: "x")
            XCTFail("expected an error")
        } catch {
            XCTAssertEqual(error, .server(status: 401, detail: "Wrong email or password. Try again."))
            XCTAssertEqual(error.localizedDescription, "Wrong email or password. Try again.")
        }
    }

    func testValidationListsAreReadable() {
        let data = Data("{\"detail\":[{\"loc\":[\"body\",\"email\"],\"msg\":\"field required\"}]}".utf8)
        XCTAssertEqual(AuthClient.detail(of: data), "email: field required")
    }

    func testA404MeansTheServerHasNoAccounts() async {
        let transport = FakeTransport { _, _ in FakeTransport.answer(404, [:], body: "{\"detail\":\"Not Found\"}") }
        do {
            _ = try await AuthClient(transport: transport).login(server: .test, email: "a@b.test", password: "x")
            XCTFail("expected an error")
        } catch {
            XCTAssertEqual(error, .accountsOff)
        }
    }

    func testModeIsWhatTheServerAnswers() async throws {
        let gated = FakeTransport { _, _ in FakeTransport.answer(401, [:], body: "{\"detail\":\"Sign in.\"}") }
        let mode = try await AuthClient(transport: gated).mode(of: .test)
        XCTAssertEqual(mode, .accounts)

        let open = FakeTransport { request, _ in
            request.url?.path() == "/api/health" ? FakeTransport.answer(200, [:], body: "{\"status\":\"ok\"}") : FakeTransport.answer(404)
        }
        let openMode = try await AuthClient(transport: open).mode(of: .test)
        XCTAssertEqual(openMode, .open)
        XCTAssertEqual(open.requests.map { $0.url?.path() }, ["/api/auth/me", "/api/health"])

        let stranger = FakeTransport { _, _ in FakeTransport.answer(404) }
        do {
            _ = try await AuthClient(transport: stranger).mode(of: .test)
            XCTFail("a server whose /api/health is a 404 is not Teacher")
        } catch {
            guard case .unexpected(let text) = error else { return XCTFail("\(error)") }
            XCTAssertTrue(text.contains("does not answer like a Teacher server"), text)
        }
    }

    func testUnreachableIsSaid() async {
        let transport = FakeTransport { _, _ in throw URLError(.cannotConnectToHost) }
        do {
            _ = try await AuthClient(transport: transport).mode(of: .test)
            XCTFail("expected an error")
        } catch {
            guard case .unreachable = error else { return XCTFail("\(error)") }
        }
    }

    func testLogoutAndMeCarryTheToken() async throws {
        let transport = FakeTransport { request, _ in
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer tok")
            if request.url?.path() == "/api/auth/logout" { return FakeTransport.answer(204) }
            return FakeTransport.answer(200, [:], body: "{\"id\":7,\"email\":\"r@example.test\",\"created_at\":\"x\"}")
        }
        let client = AuthClient(transport: transport)
        try await client.logout(server: .test, token: "tok")
        let user = try await client.me(server: .test, token: "tok")
        XCTAssertEqual(user.email, "r@example.test")
    }
}

final class KeychainSessionStoreTests: XCTestCase {
    func testSaveLoadDelete() throws {
        let store = KeychainSessionStore(service: "dev.airistotle.reader.tests", account: UUID().uuidString)
        defer { try? store.delete() }
        XCTAssertNil(try store.load())
        let session = StoredSession(server: .test, kind: .account(
            token: "tok", user: AccountUser(id: 1, email: "r@example.test", createdAt: "x"), expiresAt: "y"
        ))
        try store.save(session)
        XCTAssertEqual(try store.load(), session)
        let open = StoredSession(server: .test, kind: .open)
        try store.save(open) // an update, not a second item
        XCTAssertEqual(try store.load(), open)
        try store.delete()
        XCTAssertNil(try store.load())
        try store.delete() // deleting nothing is fine
    }
}

@MainActor
final class AppModelTests: XCTestCase {
    private func makeModel(_ transport: FakeTransport, store: EphemeralSessionStore = EphemeralSessionStore()) -> AppModel {
        let defaults = UserDefaults(suiteName: "AiristotleTests-\(UUID().uuidString)")!
        return AppModel(transport: transport, store: store, cache: ResponseCache(root: TestFiles.folder()), defaults: defaults)
    }

    /// Let the model's event loop run.
    private func settle() async {
        for _ in 0..<20 { await Task.yield() }
        try? await Task.sleep(for: .milliseconds(50))
    }

    func testStartsSignedOutWithoutASavedSession() async {
        let model = makeModel(FakeTransport())
        XCTAssertEqual(model.phase, .launching)
        await model.restore()
        XCTAssertEqual(model.phase, .signedOut(notice: nil))
        XCTAssertEqual(model.lastServerText, "https://airistotle.fly.dev")
    }

    func testSignInStoresTheSessionAndConnectsTheGateway() async throws {
        let store = EphemeralSessionStore()
        let model = makeModel(FakeTransport { _, _ in FakeTransport.answer(200, [:], body: sessionJSON) }, store: store)
        try await model.signIn(server: "http://127.0.0.1:8031", email: "reader@example.test", password: "pw")
        guard case .signedIn(let session) = model.phase else { return XCTFail("\(model.phase)") }
        XCTAssertEqual(session.token, "tok-123")
        XCTAssertEqual(session.email, "reader@example.test")
        XCTAssertEqual(try store.load(), session)
        XCTAssertEqual(model.gateway.connection?.token, "tok-123")
        XCTAssertEqual(model.gateway.connection?.server.description, "http://127.0.0.1:8031")
        XCTAssertEqual(model.lastServerText, "http://127.0.0.1:8031")
    }

    func testSignInRefusesBadInputBeforeAskingTheServer() async {
        let transport = FakeTransport()
        let model = makeModel(transport)
        do {
            try await model.signIn(server: "http://example.com", email: "a@b.test", password: "pw")
            XCTFail("plain http to a public host is refused")
        } catch {}
        do {
            try await model.signIn(server: "https://example.test", email: " ", password: "")
            XCTFail("an empty email is refused")
        } catch {}
        XCTAssertTrue(transport.requests.isEmpty)
    }

    func testConnectingWithoutAnAccountIsDecidedByTheServer() async throws {
        let gated = makeModel(FakeTransport { _, _ in FakeTransport.answer(401) })
        do {
            try await gated.connectWithoutAccount(server: "https://example.test")
            XCTFail("a server with accounts refuses")
        } catch {
            XCTAssertEqual(error as? AuthError, .accountsRequired)
        }

        let open = makeModel(FakeTransport { request, _ in
            request.url?.path() == "/api/health" ? FakeTransport.answer(200) : FakeTransport.answer(404)
        })
        try await open.connectWithoutAccount(server: "https://example.test")
        guard case .signedIn(let session) = open.phase else { return XCTFail("\(open.phase)") }
        XCTAssertEqual(session.kind, .open)
        XCTAssertNil(open.gateway.connection?.token)
    }

    func testARejectedSessionIsForgottenAndTheSignInScreenSaysWhy() async throws {
        let store = EphemeralSessionStore()
        let model = makeModel(FakeTransport { _, _ in FakeTransport.answer(200, [:], body: sessionJSON) }, store: store)
        try await model.signIn(server: "https://example.test", email: "reader@example.test", password: "pw")

        model.handle(.sessionRejected(returnTo: "/artifacts/s"))
        guard case .signedOut(let notice) = model.phase else { return XCTFail("\(model.phase)") }
        XCTAssertEqual(notice, "Your session on example.test has ended. Sign in again.")
        XCTAssertNil(try store.load())
        XCTAssertNil(model.gateway.connection)

        // Signing in again goes back to where the reader was.
        try await model.signIn(server: "https://example.test", email: "reader@example.test", password: "pw")
        XCTAssertEqual(model.startTarget, "/artifacts/s")
    }

    func testAGateway401ReachesTheModel() async throws {
        let transport = FakeTransport { _, _ in FakeTransport.answer(200, [:], body: sessionJSON) }
        let model = makeModel(transport)
        try await model.signIn(server: "https://example.test", email: "reader@example.test", password: "pw")
        transport.setHandler { _, _ in FakeTransport.answer(401, [:], body: "{\"detail\":\"Sign in first.\"}") }
        _ = await model.gateway.reply(to: GatewayRequest(method: "GET", target: "/api/library"))
        await settle()
        guard case .signedOut = model.phase else { return XCTFail("\(model.phase)") }
    }

    func testSignOutEndsTheServerSessionAndForgetsIt() async throws {
        let store = EphemeralSessionStore()
        let transport = FakeTransport { _, _ in FakeTransport.answer(200, [:], body: sessionJSON) }
        let model = makeModel(transport, store: store)
        try await model.signIn(server: "https://example.test", email: "reader@example.test", password: "pw")
        transport.setHandler { request, _ in
            XCTAssertEqual(request.url?.path(), "/api/auth/logout")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer tok-123")
            return FakeTransport.answer(204)
        }
        let warning = await model.signOut()
        XCTAssertNil(warning)
        XCTAssertEqual(model.phase, .signedOut(notice: nil))
        XCTAssertNil(try store.load())
        XCTAssertNil(model.gateway.connection)
    }

    func testSigningOutOfflineSaysTheServerSessionLivesOn() async throws {
        let store = EphemeralSessionStore()
        let transport = FakeTransport { _, _ in FakeTransport.answer(200, [:], body: sessionJSON) }
        let model = makeModel(transport, store: store)
        try await model.signIn(server: "https://example.test", email: "reader@example.test", password: "pw")
        transport.setHandler { _, _ in throw URLError(.notConnectedToInternet) }
        let result = await model.signOut()
        let warning = try XCTUnwrap(result)
        XCTAssertTrue(warning.contains("could not be told"), warning)
        XCTAssertNil(try store.load(), "the phone forgets the session anyway")
    }

    func testRestoreChecksTheSessionWithTheServer() async throws {
        let store = EphemeralSessionStore()
        try store.save(StoredSession(server: .test, kind: .account(
            token: "old", user: AccountUser(id: 1, email: "r@example.test", createdAt: "x"), expiresAt: "y"
        )))
        let model = makeModel(FakeTransport { _, _ in FakeTransport.answer(401) }, store: store)
        await model.restore()
        guard case .signedOut(let notice) = model.phase else { return XCTFail("\(model.phase)") }
        XCTAssertEqual(notice, "Your session on example.test has ended. Sign in again.")
    }

    func testRestoreOfflineKeepsTheSession() async throws {
        let store = EphemeralSessionStore()
        try store.save(StoredSession(server: .test, kind: .account(
            token: "tok", user: AccountUser(id: 1, email: "r@example.test", createdAt: "x"), expiresAt: "y"
        )))
        let model = makeModel(FakeTransport { _, _ in throw URLError(.notConnectedToInternet) }, store: store)
        await model.restore()
        guard case .signedIn = model.phase else { return XCTFail("\(model.phase)") }
        guard case .offline = model.connectivity else { return XCTFail("\(model.connectivity)") }
    }

    func testConnectivityAndPageOriginFollowTheGateway() {
        let model = makeModel(FakeTransport())
        model.handle(.serverUnreachable("down"))
        guard case .offline(let reason, _) = model.connectivity else { return XCTFail("expected offline") }
        XCTAssertEqual(reason, "down")
        let when = Date(timeIntervalSince1970: 1_000)
        model.handle(.pageShown(target: "/", source: .cache(storedAt: when)))
        XCTAssertEqual(model.pageOrigin, .savedCopy(storedAt: when))
        model.handle(.serverReachable)
        model.handle(.pageShown(target: "/", source: .server))
        XCTAssertEqual(model.connectivity, .online)
        XCTAssertEqual(model.pageOrigin, .server)
    }

    func testExpiryDatesFromTheServerAreRead() throws {
        let precise = try XCTUnwrap(SettingsView.date("2026-12-25T10:00:00.123456+00:00"))
        let plain = try XCTUnwrap(SettingsView.date("2026-12-25T10:00:00+00:00"))
        XCTAssertLessThan(abs(precise.timeIntervalSince(plain)), 1)
        XCTAssertEqual(plain.timeIntervalSince1970, 1_798_192_800)
    }
}

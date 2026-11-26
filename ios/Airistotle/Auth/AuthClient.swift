import Foundation

/// The signed-in account, as `/api/auth/*` describes it (`teacher.web.auth.UserOut`).
struct AccountUser: Codable, Sendable, Equatable {
    var id: Int
    var email: String
    var createdAt: String

    enum CodingKeys: String, CodingKey {
        case id, email
        case createdAt = "created_at"
    }
}

/// A new session (`teacher.web.auth.SessionOut`). The token is shown once and goes straight
/// to the Keychain.
struct NewSession: Codable, Sendable, Equatable {
    var token: String
    var expiresAt: String
    var user: AccountUser

    enum CodingKeys: String, CodingKey {
        case token, user
        case expiresAt = "expires_at"
    }
}

/// Whether a server has accounts, decided by what it answers.
enum ServerMode: Sendable, Equatable {
    /// The account gate is on (`$TEACHER_PASSWORD` set): sign in or register.
    case accounts
    /// No accounts (`/api/auth/*` does not exist): the app is open to whoever reaches it.
    case open
}

enum AuthError: Error, Equatable, LocalizedError {
    /// The server answered with an error; `detail` is its own message.
    case server(status: Int, detail: String)
    /// The server has no accounts (`/api/auth/*` is a 404).
    case accountsOff
    /// The server requires an account, so it cannot be used without one.
    case accountsRequired
    /// The server could not be reached.
    case unreachable(String)
    /// The server's answer was not what a Teacher server sends.
    case unexpected(String)

    var errorDescription: String? {
        switch self {
        case .server(_, let detail):
            detail
        case .accountsOff:
            "This server runs without accounts. Use “Connect without an account”."
        case .accountsRequired:
            "This server requires an account. Sign in, or create an account with the access code."
        case .unreachable(let reason):
            "The server could not be reached: \(reason)"
        case .unexpected(let what):
            what
        }
    }
}

/// The JSON sign-in routes of `teacher.web.auth`, called natively (never through the web view).
struct AuthClient: Sendable {
    /// The label the server stores with each session ("which device is this").
    static let clientLabel = "Airistotle (iPhone)"

    let transport: any HTTPTransport

    func login(server: ServerAddress, email: String, password: String) async throws(AuthError) -> NewSession {
        let body = ["email": email, "password": password, "client": Self.clientLabel]
        return try await session(server: server, path: "/api/auth/login", body: body)
    }

    func register(server: ServerAddress, email: String, password: String, accessCode: String) async throws(AuthError) -> NewSession {
        let body = ["email": email, "password": password, "access_code": accessCode, "client": Self.clientLabel]
        return try await session(server: server, path: "/api/auth/register", body: body)
    }

    /// The account of a token; `.server(401, ...)` when the server no longer accepts it.
    func me(server: ServerAddress, token: String) async throws(AuthError) -> AccountUser {
        var request = try Self.request(server, "/api/auth/me", method: "GET")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let (response, data) = try await send(request)
        guard response.statusCode == 200 else { throw Self.failure(response, data, notFound: .accountsOff) }
        return try Self.decode(AccountUser.self, data, what: "GET /api/auth/me")
    }

    /// End the session on the server (204). A 401 means it had already ended, which is fine.
    func logout(server: ServerAddress, token: String) async throws(AuthError) {
        var request = try Self.request(server, "/api/auth/logout", method: "POST")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let (response, data) = try await send(request)
        guard response.statusCode == 204 || response.statusCode == 401 else {
            throw Self.failure(response, data, notFound: .accountsOff)
        }
    }

    /// Whether `server` has accounts: `GET /api/auth/me` without a session is a 401 behind
    /// the account gate and a 404 without it, where `GET /api/health` must then answer 200.
    func mode(of server: ServerAddress) async throws(AuthError) -> ServerMode {
        let (response, data) = try await send(try Self.request(server, "/api/auth/me", method: "GET"))
        switch response.statusCode {
        case 401:
            return .accounts
        case 404:
            let (health, healthData) = try await send(try Self.request(server, "/api/health", method: "GET"))
            guard health.statusCode == 200 else {
                throw .unexpected(
                    "\(server.host) does not answer like a Teacher server "
                        + "(GET /api/health: HTTP \(health.statusCode) \(Self.snippet(healthData)))."
                )
            }
            return .open
        default:
            throw .unexpected(
                "\(server.host) does not answer like a Teacher server "
                    + "(GET /api/auth/me without a session: HTTP \(response.statusCode) \(Self.snippet(data)))."
            )
        }
    }

    // MARK: Plumbing

    private func session(server: ServerAddress, path: String, body: [String: String]) async throws(AuthError) -> NewSession {
        var request = try Self.request(server, path, method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        do {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        } catch {
            throw .unexpected("Could not encode the request: \(error.localizedDescription)")
        }
        let (response, data) = try await send(request)
        guard (200...299).contains(response.statusCode) else {
            throw Self.failure(response, data, notFound: .accountsOff)
        }
        return try Self.decode(NewSession.self, data, what: "POST \(path)")
    }

    private func send(_ request: URLRequest) async throws(AuthError) -> (HTTPURLResponse, Data) {
        do {
            return try await transport.data(for: request)
        } catch let error as URLError where error.meansUnreachable {
            throw .unreachable(error.localizedDescription)
        } catch {
            throw .unexpected("The request to \(request.url?.host() ?? "the server") failed: \(error.localizedDescription)")
        }
    }

    private static func request(_ server: ServerAddress, _ path: String, method: String) throws(AuthError) -> URLRequest {
        guard let url = server.url(forTarget: path) else { throw .unexpected("Bad server address \(server).") }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.timeoutInterval = 30
        return request
    }

    private static func failure(_ response: HTTPURLResponse, _ data: Data, notFound: AuthError) -> AuthError {
        if response.statusCode == 404 { return notFound }
        return .server(status: response.statusCode, detail: detail(of: data) ?? "HTTP \(response.statusCode) \(snippet(data))")
    }

    /// The API's error text: `{"detail": "..."}`, or FastAPI's validation list.
    static func detail(of data: Data) -> String? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        if let text = object["detail"] as? String, !text.isEmpty { return text }
        if let list = object["detail"] as? [[String: Any]] {
            let messages = list.compactMap { item -> String? in
                guard let message = item["msg"] as? String else { return nil }
                let location = (item["loc"] as? [Any])?.map { "\($0)" }.filter { $0 != "body" }.joined(separator: ".") ?? ""
                return location.isEmpty ? message : "\(location): \(message)"
            }
            if !messages.isEmpty { return messages.joined(separator: "; ") }
        }
        return nil
    }

    private static func decode<T: Decodable>(_ type: T.Type, _ data: Data, what: String) throws(AuthError) -> T {
        do {
            return try JSONDecoder().decode(type, from: data)
        } catch {
            throw .unexpected("The server's answer to \(what) could not be read (\(error.localizedDescription)).")
        }
    }

    private static func snippet(_ data: Data) -> String {
        String(bytes: data.prefix(160), encoding: .utf8) ?? "(\(data.count) bytes that are not text)"
    }
}

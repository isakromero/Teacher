import Foundation
import Synchronization

/// Who the gateway talks to: the server, the session token (nil when the server runs without
/// accounts) and the cache namespace of this server and account.
struct GatewayConnection: Sendable, Equatable {
    var server: ServerAddress
    var token: String?
    var cacheNamespace: String

    init(server: ServerAddress, token: String?, account: String?) {
        self.server = server
        self.token = token
        self.cacheNamespace = ResponseCache.namespace(server: server, account: account)
    }
}

/// Everything between the page and the server.
///
/// For each request the web view makes: a local route may answer it (`GatewayRoute`);
/// otherwise it is forwarded to the server with the session's bearer token and the answer is
/// passed back as it arrives (status, headers, body), and a successful GET is saved
/// (`ResponseCache`). When the server cannot be reached, an offline route may answer
/// (`OfflineRoute`), then a GET is answered from the saved copy; anything else gets an
/// explicit error saying the phone is offline and nothing was sent or saved.
final class Gateway: Sendable {
    /// A reply header naming where it came from when it is not the server (`cache`).
    static let sourceHeader = "X-Airistotle-Source"
    /// For a saved copy: when it was saved (ISO 8601).
    static let storedAtHeader = "X-Airistotle-Stored-At"

    let cache: ResponseCache
    private let transport: any HTTPTransport
    private let plugins: Mutex<Plugins>
    private let events: @Sendable (GatewayEvent) -> Void
    private let connectionState = Mutex<GatewayConnection?>(nil)

    private struct Plugins {
        var routes: [any GatewayRoute]
        var offlineRoutes: [any OfflineRoute]
        var observers: [any GatewayObserver]
    }

    init(
        transport: any HTTPTransport,
        cache: ResponseCache,
        routes: [any GatewayRoute] = [],
        offlineRoutes: [any OfflineRoute] = [],
        observers: [any GatewayObserver] = [],
        events: @escaping @Sendable (GatewayEvent) -> Void
    ) {
        self.transport = transport
        self.cache = cache
        self.plugins = Mutex(Plugins(
            routes: [AuthPagesRoute(events: events)] + routes, offlineRoutes: offlineRoutes, observers: observers
        ))
        self.events = events
    }

    /// Add a route, an offline route and/or an observer after the gateway exists (they may
    /// need the gateway themselves, as the offline service does).
    func install(route: (any GatewayRoute)? = nil, offlineRoute: (any OfflineRoute)? = nil, observer: (any GatewayObserver)? = nil) {
        plugins.withLock { plugins in
            if let route { plugins.routes.append(route) }
            if let offlineRoute { plugins.offlineRoutes.append(offlineRoute) }
            if let observer { plugins.observers.append(observer) }
        }
    }

    private var routes: [any GatewayRoute] { plugins.withLock { $0.routes } }
    private var offlineRoutes: [any OfflineRoute] { plugins.withLock { $0.offlineRoutes } }
    private var observers: [any GatewayObserver] { plugins.withLock { $0.observers } }

    /// Tell the app something happened (the offline service reports through the same events).
    func emit(_ event: GatewayEvent) {
        events(event)
    }

    /// The connection used from the next request on (nil: signed out, nothing is forwarded).
    var connection: GatewayConnection? {
        get { connectionState.withLock { $0 } }
        set { connectionState.withLock { $0 = newValue } }
    }

    // MARK: Answering a request

    func reply(to request: GatewayRequest) async -> GatewayReply {
        let reply = await answer(request)
        if request.isMainFrame { events(.pageShown(target: request.target, source: reply.source)) }
        if GradedAnswer.isSubmission(request) {
            // Whoever answered (the server, or the phone's reading engine): the grade is read
            // from what the page receives.
            let events = events
            return GradedAnswer.watch(reply) { events(.answerGraded($0)) }
        }
        return reply
    }

    private func answer(_ request: GatewayRequest) async -> GatewayReply {
        guard let connection else {
            events(.sessionRejected(returnTo: nil))
            return Self.notConnected(request)
        }
        let context = GatewayContext(connection: connection, cache: cache)
        for route in routes {
            if let reply = await route.reply(to: request, context: context) { return reply }
        }
        guard let urlRequest = serverRequest(for: request, connection: connection) else {
            return .jsonError(400, "The app could not build a server address for \(request.target).")
        }

        let response: TransportResponse
        do {
            response = try await transport.send(urlRequest, followRedirects: !request.isNavigation)
        } catch let error as URLError where error.meansUnreachable {
            events(.serverUnreachable(error.localizedDescription))
            return await offlineReply(to: request, context: context, failure: error)
        } catch is CancellationError {
            return .jsonError(499, "The page stopped waiting for this request.")
        } catch let error as URLError where error.code == .cancelled {
            return .jsonError(499, "The page stopped waiting for this request.")
        } catch {
            return Self.failed(request, server: connection.server, error: error)
        }
        events(.serverReachable)
        return await passOn(response, for: request, context: context)
    }

    /// The URLRequest to the server for a page's request.
    func serverRequest(for request: GatewayRequest, connection: GatewayConnection) -> URLRequest? {
        guard let url = connection.server.url(forTarget: request.target) else { return nil }
        var urlRequest = URLRequest(url: url)
        urlRequest.httpMethod = request.method
        for (name, value) in request.headers where !Self.droppedRequestHeaders.contains(name.lowercased()) {
            urlRequest.setValue(value, forHTTPHeaderField: name)
        }
        if let token = connection.token {
            urlRequest.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if request.method != "GET", request.method != "HEAD" {
            urlRequest.httpBody = request.body ?? Data()
        }
        return urlRequest
    }

    /// Headers of the page's request that are not sent on. `Origin` and `Referer` name the
    /// app's own `teacher://` origin, which the server's cross-site check would refuse; the
    /// session is the bearer token, not a cookie; conditional headers are dropped so the
    /// server always sends a full body the cache can keep.
    static let droppedRequestHeaders: Set<String> = [
        "authorization", "cookie", "origin", "referer", "host", "connection", "content-length",
        "accept-encoding", "if-none-match", "if-modified-since", "if-match", "if-unmodified-since",
        "if-range", "upgrade-insecure-requests", "keep-alive", "transfer-encoding"
    ]

    /// Response headers not passed to the web view: URLSession already decoded the body (so
    /// its encoding and length no longer apply), and cookies and transport headers are the
    /// server's business.
    static let droppedResponseHeaders: Set<String> = [
        "content-encoding", "content-length", "transfer-encoding", "connection", "keep-alive",
        "set-cookie", "strict-transport-security", "alt-svc"
    ]

    // MARK: The server's answer

    private func passOn(_ response: TransportResponse, for request: GatewayRequest, context: GatewayContext) async -> GatewayReply {
        let http = response.response
        let status = http.statusCode
        var headers: [String: String] = [:]
        for (key, value) in http.allHeaderFields {
            guard let name = key as? String, let value = value as? String,
                  !Self.droppedResponseHeaders.contains(name.lowercased()) else { continue }
            headers[name] = value
        }
        for observer in observers {
            await observer.serverReplied(to: request, status: status, headers: headers, context: context)
        }

        if status == 401 {
            // The account gate refused the token (expired or signed out elsewhere). A script
            // still gets the server's 401 (it shows the error), and the app asks to sign in.
            events(.sessionRejected(returnTo: request.isMainFrame ? request.target : nil))
            if request.isNavigation {
                response.body.cancelIgnoringResult()
                return .htmlPage(401, title: "Sign in again", message: "Your session has ended. Sign in again to continue.")
            }
            return GatewayReply(status: status, headers: headers, body: response.body, source: .server)
        }

        if (300...399).contains(status), status != 304 {
            return redirect(status: status, headers: headers, for: request, context: context, body: response.body)
        }

        var body = response.body
        if shouldCache(request, status: status, headers: headers) {
            do {
                let writer = try cache.writer(
                    namespace: context.connection.cacheNamespace, target: request.target,
                    status: status, headers: headers
                )
                body = Self.tee(body, into: writer)
            } catch {
                // Not saving is not a failure of the request; the page still gets its answer.
                // It is reported where it can be seen (the debugger console in development).
                print("Airistotle: could not save \(request.target) for offline use: \(error)")
            }
        }
        return GatewayReply(status: status, headers: headers, body: body, source: .server)
    }

    private func redirect(
        status: Int,
        headers: [String: String],
        for request: GatewayRequest,
        context: GatewayContext,
        body: AsyncThrowingStream<Data, any Error>
    ) -> GatewayReply {
        body.cancelIgnoringResult()
        let server = context.connection.server
        let location = headers.first { $0.key.caseInsensitiveCompare("Location") == .orderedSame }?.value
        guard let location, let resolved = URL(string: location, relativeTo: server.url(forTarget: request.target)) else {
            return .jsonError(502, "The server answered \(status) with no usable Location for \(request.target).")
        }
        let absolute = resolved.absoluteURL
        if server.owns(absolute), let appURL = AppURL.appURL(forServerURL: absolute, server: server),
           let target = AppURL.target(of: appURL) {
            let path = String(target.prefix { $0 != "?" })
            if AuthPagesRoute.signInPaths.contains(path) {
                // The account gate sends a page without a valid session to /login?next=...
                let next = URLComponents(string: "x://y\(target)")?.queryItems?.first { $0.name == "next" }?.value
                events(.sessionRejected(returnTo: AuthPagesRoute.safeReturn(next) ?? (request.isMainFrame ? request.target : nil)))
                if request.isNavigation {
                    return .htmlPage(401, title: "Sign in again", message: "Your session has ended. Sign in again to continue.")
                }
                return .jsonError(401, "Your session has ended. Sign in again in the app.")
            }
            if request.isNavigation { return .redirectPage(to: appURL) }
        }
        if request.isNavigation {
            return .htmlPage(
                502, title: "Redirected away",
                message: "The server sent this page to an address outside Teacher (\(absolute.absoluteString)). "
                    + "It opens in Safari if you follow the link.",
                actions: [("Open in Safari", absolute.absoluteString)]
            )
        }
        return .jsonError(502, "The server redirected \(request.target) to \(absolute.absoluteString), which the app does not follow.")
    }

    /// Successful GETs are saved, except streams, responses the server marks `no-store`, the
    /// sign-in routes and partial content.
    func shouldCache(_ request: GatewayRequest, status: Int, headers: [String: String]) -> Bool {
        guard request.method == "GET", status == 200 else { return false }
        if request.path.hasPrefix("/api/auth/") { return false }
        let lowered = Dictionary(headers.map { ($0.key.lowercased(), $0.value.lowercased()) }) { first, _ in first }
        if lowered["cache-control"]?.contains("no-store") == true { return false }
        if lowered["content-type"]?.hasPrefix("text/event-stream") == true { return false }
        return true
    }

    /// Pass `body` through unchanged while writing it to `writer`; commit when it ended
    /// cleanly, discard when it failed or the page stopped reading.
    static func tee(_ body: AsyncThrowingStream<Data, any Error>, into writer: CacheWriter) -> AsyncThrowingStream<Data, any Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var writing = true
                    for try await chunk in body {
                        try Task.checkCancellation()
                        if writing {
                            do { try writer.append(chunk) } catch { writing = false; writer.discard() }
                        }
                        continuation.yield(chunk)
                    }
                    if writing { try? writer.commit() }
                    continuation.finish()
                } catch {
                    writer.discard()
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { termination in
                if case .cancelled = termination {
                    task.cancel()
                    writer.discard()
                }
            }
        }
    }

    // MARK: When the server cannot be reached

    private func offlineReply(to request: GatewayRequest, context: GatewayContext, failure: URLError) async -> GatewayReply {
        for route in offlineRoutes {
            if let reply = await route.reply(to: request, context: context, failure: failure) { return reply }
        }
        let host = context.connection.server.host
        if request.isReadOnly, let saved = cache.lookup(namespace: context.connection.cacheNamespace, target: request.target) {
            var headers = saved.metadata.headers
            headers[Self.sourceHeader] = "cache"
            headers[Self.storedAtHeader] = ISO8601DateFormatter().string(from: saved.metadata.storedAt)
            headers["Content-Length"] = String(saved.metadata.byteCount)
            let body: AsyncThrowingStream<Data, any Error> = request.method == "HEAD"
                ? .single(Data()) : ResponseCache.bodyStream(of: saved.bodyFile)
            return GatewayReply(
                status: saved.metadata.status, headers: headers, body: body,
                source: .cache(storedAt: saved.metadata.storedAt)
            )
        }
        let reason = failure.localizedDescription
        if request.isReadOnly {
            let message = "The phone is offline: \(host) could not be reached (\(reason)). "
                + "This has not been saved on this phone, so it cannot be shown."
            if request.isNavigation {
                return .htmlPage(503, title: "Offline", message: message, actions: [("Try again", request.target)])
            }
            return .jsonError(503, message)
        }
        let message = "The phone is offline: \(host) could not be reached (\(reason)). "
            + "This was not sent to the server and nothing was saved."
        if request.isNavigation {
            return .htmlPage(503, title: "Offline", message: message)
        }
        return .jsonError(503, message)
    }

    // MARK: Direct requests (the app's own, not the page's)

    enum FetchError: Error, LocalizedError {
        case notConnected
        case unreachable(URLError)
        case failed(String)

        var errorDescription: String? {
            switch self {
            case .notConnected: "The app is not connected to a server."
            case .unreachable(let error): "The server could not be reached (\(error.localizedDescription))."
            case .failed(let reason): reason
            }
        }
    }

    /// One request to the server with the session, whole body; no route, no cache. Reports
    /// reachability and a refused session like a page's request does.
    func fetch(
        _ method: String, _ target: String, json: JSONValue? = nil, accept: String = "application/json"
    ) async throws(FetchError) -> (status: Int, headers: [String: String], body: Data) {
        guard let connection else { throw .notConnected }
        var request = GatewayRequest(method: method, target: target, headers: ["Accept": accept])
        if let json {
            request.body = json.data()
            request.headers["Content-Type"] = "application/json"
        }
        guard let urlRequest = serverRequest(for: request, connection: connection) else {
            throw .failed("The app could not build a server address for \(target).")
        }
        let response: TransportResponse
        do {
            response = try await transport.send(urlRequest, followRedirects: false)
        } catch let error as URLError where error.meansUnreachable {
            events(.serverUnreachable(error.localizedDescription))
            throw .unreachable(error)
        } catch {
            throw .failed("\(method) \(target) failed before the server answered (\(error.localizedDescription)).")
        }
        events(.serverReachable)
        var body = Data()
        do {
            for try await chunk in response.body { body.append(chunk) }
        } catch let error as URLError where error.meansUnreachable {
            events(.serverUnreachable(error.localizedDescription))
            throw .unreachable(error)
        } catch {
            throw .failed("\(method) \(target): the answer broke off (\(error.localizedDescription)).")
        }
        var headers: [String: String] = [:]
        for (key, value) in response.response.allHeaderFields {
            if let key = key as? String, let value = value as? String { headers[key] = value }
        }
        let status = response.response.statusCode
        let location = headers.first { $0.key.caseInsensitiveCompare("Location") == .orderedSame }?.value ?? ""
        if status == 401 || ((300...399).contains(status) && location.hasPrefix("/login")) {
            events(.sessionRejected(returnTo: nil))
        }
        return (status, headers, body)
    }

    // MARK: Errors

    static func notConnected(_ request: GatewayRequest) -> GatewayReply {
        let message = "The app is not connected to a server. Sign in again in the app; nothing was sent."
        if request.isNavigation { return .htmlPage(401, title: "Not signed in", message: message) }
        return .jsonError(401, message)
    }

    static func failed(_ request: GatewayRequest, server: ServerAddress, error: any Error) -> GatewayReply {
        let message = "The request to \(server.host) failed before the server answered "
            + "(\(error.localizedDescription)). Nothing was saved."
        if request.isNavigation { return .htmlPage(502, title: "Could not load this page", message: message) }
        return .jsonError(502, message)
    }
}

extension AsyncThrowingStream where Element == Data, Failure == any Error {
    /// Stop a body nobody will read (the transport then cancels its request).
    func cancelIgnoringResult() {
        let task = Task { for try await _ in self { break } }
        task.cancel()
    }
}

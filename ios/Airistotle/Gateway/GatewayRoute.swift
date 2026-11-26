import Foundation

/// What a route knows about the current connection.
struct GatewayContext: Sendable {
    var connection: GatewayConnection
    var cache: ResponseCache
}

/// A request the phone answers itself, before the server is asked.
///
/// Routes are consulted in order; the first non-nil reply wins. Phase 1 uses this for the
/// sign-in pages the app replaces with native screens (`AuthPagesRoute`). Phase 2 plugs in
/// here to answer saved pages from local storage or grade an answer on the phone.
protocol GatewayRoute: Sendable {
    var name: String { get }
    func reply(to request: GatewayRequest, context: GatewayContext) async -> GatewayReply?
}

/// A request the phone may answer when the server could not be reached, consulted before the
/// response cache. A route that cannot answer returns nil and the gateway says it is offline.
protocol OfflineRoute: Sendable {
    var name: String { get }
    func reply(to request: GatewayRequest, context: GatewayContext, failure: URLError) async -> GatewayReply?
}

/// Something that wants to know about every reply the server gave (phase 2: keeping saved
/// pages fresh, recording what was synced). Called once the head arrived; the body is not
/// passed, so an observer cannot hold up a streamed reply.
protocol GatewayObserver: Sendable {
    func serverReplied(to request: GatewayRequest, status: Int, headers: [String: String], context: GatewayContext) async
}

/// What the gateway tells the app.
enum GatewayEvent: Sendable, Equatable {
    /// The server answered a request.
    case serverReachable
    /// The server could not be reached (the reason, as URLSession described it).
    case serverUnreachable(String)
    /// The server refused the session (401, or a redirect to its sign-in page): sign in
    /// again, then go to `returnTo` (an app target) if there is one.
    case sessionRejected(returnTo: String?)
    /// The page asked to sign out (the header's "Sign out" button posts to /logout).
    case signOutRequested
    /// A page (main frame) was shown: from the server, a saved copy or the phone itself.
    case pageShown(target: String, source: ReplySource)
    /// A checkpoint answer was graded (by the server or on the phone), read from the reply
    /// the page received (`GradedAnswer`).
    case answerGraded(AnswerOutcome)
}

/// The web client's own sign-in pages (`/login`, `/register`, `/logout`) are replaced by the
/// app's native screens and never shown or forwarded.
struct AuthPagesRoute: GatewayRoute {
    let name = "auth-pages"
    let events: @Sendable (GatewayEvent) -> Void

    static let signInPaths: Set<String> = ["/login", "/register"]
    static let signOutPath = "/logout"

    func reply(to request: GatewayRequest, context: GatewayContext) async -> GatewayReply? {
        let path = request.path
        if Self.signInPaths.contains(path) {
            // static/js/api.js sends the page to /login?next=<here> after a 401.
            let next = request.queryItems.first { $0.name == "next" }?.value
            events(.sessionRejected(returnTo: Self.safeReturn(next)))
            return .htmlPage(
                401,
                title: "Sign in again",
                message: "The server asked you to sign in again. The app's sign-in screen opens now.",
                source: .local(name)
            )
        }
        if path == Self.signOutPath {
            events(.signOutRequested)
            return .htmlPage(200, title: "Signing out", message: "Signing out of Airistotle…", source: .local(name))
        }
        return nil
    }

    /// A same-site relative path worth returning to after signing in (as the server's
    /// `safe_next`): one leading slash, no scheme or host, not a sign-in page.
    static func safeReturn(_ value: String?) -> String? {
        guard let value, value.hasPrefix("/"), !value.hasPrefix("//"), !value.contains("\\"),
              !value.unicodeScalars.contains(where: { $0.value < 0x20 || $0.value == 0x7F })
        else { return nil }
        let path = String(value.prefix { $0 != "?" && $0 != "#" })
        if signInPaths.contains(path) || path == signOutPath { return nil }
        return value
    }
}

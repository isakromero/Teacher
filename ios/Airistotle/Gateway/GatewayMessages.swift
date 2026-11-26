import Foundation

/// The app's own origin. The web view loads everything from `teacher://app/...`, and the
/// gateway answers every such request (`SchemeHandler`).
enum AppURL {
    static let scheme = "teacher"
    static let host = "app"
    static let root = URL(string: "\(scheme)://\(host)/")!

    /// The app URL of a target (`/path?query`). Targets always start with "/".
    static func url(forTarget target: String) -> URL? {
        guard target.hasPrefix("/") else { return nil }
        return URL(string: "\(scheme)://\(host)\(target)")
    }

    /// The target (`/path?query`, percent-encoding kept) of an app URL; nil for any other URL.
    static func target(of url: URL) -> String? {
        guard url.scheme?.lowercased() == scheme, url.host()?.lowercased() == host,
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        else { return nil }
        let path = components.percentEncodedPath.isEmpty ? "/" : components.percentEncodedPath
        if let query = components.percentEncodedQuery { return "\(path)?\(query)" }
        return path
    }

    /// The app URL for a link that points at the server itself (an absolute link to the same
    /// origin), so it stays inside the app.
    static func appURL(forServerURL url: URL, server: ServerAddress) -> URL? {
        guard server.owns(url), let components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        else { return nil }
        var target = components.percentEncodedPath.isEmpty ? "/" : components.percentEncodedPath
        if let query = components.percentEncodedQuery { target += "?\(query)" }
        var result = Self.url(forTarget: target)
        if let fragment = components.percentEncodedFragment, let base = result {
            result = URL(string: "\(base.absoluteString)#\(fragment)")
        }
        return result
    }
}

/// One request the page made, as the gateway sees it.
struct GatewayRequest: Sendable, Equatable {
    var method: String
    /// The path and query, e.g. `/api/artifacts/x?full=1` (percent-encoding kept).
    var target: String
    /// The page's headers (names as WebKit sent them).
    var headers: [String: String]
    var body: Data?
    /// A document load (a page or a frame), not a script's `fetch`.
    var isNavigation: Bool
    /// The document load of the main frame (what the person sees as "the page").
    var isMainFrame: Bool

    init(
        method: String,
        target: String,
        headers: [String: String] = [:],
        body: Data? = nil,
        isNavigation: Bool = false,
        isMainFrame: Bool = false
    ) {
        self.method = method.uppercased()
        self.target = target
        self.headers = headers
        self.body = body
        self.isNavigation = isNavigation
        self.isMainFrame = isMainFrame
    }

    /// The request of a web view load; nil when it is not an app URL. `body` is passed in
    /// because WebKit may hand it over as a stream (`SchemeHandler` reads it first).
    init?(_ request: URLRequest, body: Data?) {
        guard let url = request.url, let target = AppURL.target(of: url) else { return nil }
        let headers = request.allHTTPHeaderFields ?? [:]
        let accept = headers.first { $0.key.caseInsensitiveCompare("Accept") == .orderedSame }?.value ?? ""
        let method = request.httpMethod ?? "GET"
        // WebKit asks for text/html first on every document load; scripts' fetch calls do not.
        let navigation = accept.hasPrefix("text/html")
        self.init(
            method: method,
            target: target,
            headers: headers,
            body: body,
            isNavigation: navigation,
            isMainFrame: navigation && request.mainDocumentURL == url
        )
    }

    /// The path without the query.
    var path: String {
        String(target.prefix { $0 != "?" })
    }

    /// The query items (decoded), empty when there is no query.
    var queryItems: [URLQueryItem] {
        URLComponents(string: "x://y\(target)")?.queryItems ?? []
    }

    var isReadOnly: Bool { method == "GET" || method == "HEAD" }

    func header(_ name: String) -> String? {
        headers.first { $0.key.caseInsensitiveCompare(name) == .orderedSame }?.value
    }
}

/// Where a reply came from.
enum ReplySource: Sendable, Equatable {
    /// The server answered.
    case server
    /// The server could not be reached; this is the copy saved at `storedAt`.
    case cache(storedAt: Date)
    /// A route on the phone answered (`GatewayRoute`), named for diagnostics.
    case local(String)
    /// The gateway itself refused or failed; the body says why.
    case gatewayError
}

/// The answer to a `GatewayRequest`: a status, headers and a body that may still be arriving.
struct GatewayReply: Sendable {
    var status: Int
    var headers: [String: String]
    var body: AsyncThrowingStream<Data, any Error>
    var source: ReplySource

    init(status: Int, headers: [String: String], body: AsyncThrowingStream<Data, any Error>, source: ReplySource) {
        self.status = status
        self.headers = headers
        self.body = body
        self.source = source
    }

    init(status: Int, headers: [String: String], data: Data, source: ReplySource) {
        var headers = headers
        headers["Content-Length"] = String(data.count)
        self.init(status: status, headers: headers, body: .single(data), source: source)
    }

    func header(_ name: String) -> String? {
        headers.first { $0.key.caseInsensitiveCompare(name) == .orderedSame }?.value
    }

    /// The whole body (tests and small replies only).
    func collectBody() async throws -> Data {
        var data = Data()
        for try await chunk in body { data.append(chunk) }
        return data
    }

    // MARK: Replies the gateway makes itself

    /// An error in the API's own shape, `{"detail": "..."}`, which `static/js/api.js` shows.
    static func jsonError(_ status: Int, _ detail: String, source: ReplySource = .gatewayError) -> GatewayReply {
        let body = (try? JSONSerialization.data(withJSONObject: ["detail": detail], options: [.sortedKeys])) ?? Data()
        return GatewayReply(
            status: status,
            headers: ["Content-Type": "application/json", "Cache-Control": "no-store"],
            data: body,
            source: source
        )
    }

    /// A small page for a document load that the gateway answers itself.
    static func htmlPage(
        _ status: Int,
        title: String,
        message: String,
        actions: [(label: String, href: String)] = [],
        source: ReplySource = .gatewayError
    ) -> GatewayReply {
        let links = actions.map { "<a class=\"button\" href=\"\(escape($0.href))\">\(escape($0.label))</a>" }
            .joined(separator: " ")
        let html = """
        <!doctype html>
        <html lang="en"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta name="color-scheme" content="light dark">
        <title>\(escape(title))</title>
        <style>
        body{font:17px -apple-system,system-ui,sans-serif;margin:0;padding:48px 24px;
        background:#f7f5f0;color:#1c1b18;line-height:1.45}
        main{max-width:34rem;margin:0 auto}
        h1{font-size:1.35rem;margin:0 0 .75rem}
        p{margin:0 0 1rem}
        .button{display:inline-block;margin-top:.5rem;padding:.55rem 1rem;border-radius:.6rem;
        background:#28598a;color:#fff;text-decoration:none;font-weight:600}
        @media (prefers-color-scheme: dark){body{background:#141312;color:#ece9e3}
        .button{background:#8fb9e6;color:#0e1620}}
        </style></head>
        <body><main><h1>\(escape(title))</h1><p>\(escape(message))</p>\(links)</main></body></html>
        """
        return GatewayReply(
            status: status,
            headers: ["Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"],
            data: Data(html.utf8),
            source: source
        )
    }

    /// A document that replaces itself with `location` (WebKit does not follow a redirect
    /// returned by a URL scheme handler, so a navigation's redirect is done by the page).
    static func redirectPage(to location: URL) -> GatewayReply {
        let href = location.absoluteString
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.withoutEscapingSlashes]
        let literal = (try? encoder.encode(href)).flatMap { String(bytes: $0, encoding: .utf8) } ?? "\"/\""
        let html = """
        <!doctype html><html><head><meta charset="utf-8">
        <meta http-equiv="refresh" content="0;url=\(escape(href))">
        <script>location.replace(\(literal));</script></head><body></body></html>
        """
        return GatewayReply(
            status: 200,
            headers: ["Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"],
            data: Data(html.utf8),
            source: .local("redirect")
        )
    }

    static func escape(_ text: String) -> String {
        var out = ""
        for char in text {
            switch char {
            case "&": out += "&amp;"
            case "<": out += "&lt;"
            case ">": out += "&gt;"
            case "\"": out += "&quot;"
            case "'": out += "&#39;"
            default: out.append(char)
            }
        }
        return out
    }
}

extension AsyncThrowingStream where Element == Data, Failure == any Error {
    /// A body of one chunk.
    static func single(_ data: Data) -> Self {
        AsyncThrowingStream { continuation in
            if !data.isEmpty { continuation.yield(data) }
            continuation.finish()
        }
    }
}

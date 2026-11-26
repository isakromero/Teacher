import Foundation

/// The address of a Teacher server: an http(s) origin with no path, query or credentials.
///
/// The web client builds every URL from the site root (`/static/...`, `/api/...`), so a server
/// mounted under a path prefix cannot work and is refused rather than half-working. Plain http
/// is accepted only for local addresses (the app's ATS settings allow nothing else).
struct ServerAddress: Hashable, Sendable, Codable, CustomStringConvertible {
    static let defaultAddress = ServerAddress(validated: URL(string: "https://airistotle.fly.dev")!)

    /// The origin, e.g. `https://airistotle.fly.dev` or `http://127.0.0.1:8031`.
    let origin: URL

    enum ParseError: Error, Equatable, LocalizedError {
        case empty
        case notAURL(String)
        case unsupportedScheme(String)
        case hasPath(String)
        case hasCredentials
        case hasQueryOrFragment
        case plainHTTPNotLocal(String)

        var errorDescription: String? {
            switch self {
            case .empty:
                "Enter the server address, for example https://airistotle.fly.dev."
            case .notAURL(let text):
                "“\(text)” is not a web address."
            case .unsupportedScheme(let scheme):
                "The server address must start with https:// (not \(scheme)://)."
            case .hasPath(let path):
                "Enter the server's address without a path (remove “\(path)”): "
                    + "Teacher is served from the site root."
            case .hasCredentials:
                "Remove the user name or password from the server address."
            case .hasQueryOrFragment:
                "Remove the “?…” or “#…” part from the server address."
            case .plainHTTPNotLocal(let host):
                "Plain http is only allowed for a server on your local network; "
                    + "use https://\(host)."
            }
        }
    }

    private init(validated origin: URL) {
        self.origin = origin
    }

    /// Parse what a person typed. A missing scheme means https.
    init(parsing text: String) throws(ParseError) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { throw .empty }
        let withScheme = trimmed.contains("://") ? trimmed : "https://\(trimmed)"
        guard let components = URLComponents(string: withScheme),
              let scheme = components.scheme?.lowercased(),
              let host = components.host?.lowercased(), !host.isEmpty
        else { throw .notAURL(trimmed) }
        guard scheme == "https" || scheme == "http" else { throw .unsupportedScheme(scheme) }
        guard components.user == nil, components.password == nil else { throw .hasCredentials }
        guard components.query == nil, components.fragment == nil else { throw .hasQueryOrFragment }
        let path = components.path
        guard path.isEmpty || path == "/" else { throw .hasPath(path) }
        if scheme == "http", !Self.isLocal(host: host) { throw .plainHTTPNotLocal(host) }

        var clean = URLComponents()
        clean.scheme = scheme
        clean.host = host
        if let port = components.port, port != (scheme == "https" ? 443 : 80) {
            clean.port = port
        }
        guard let url = clean.url else { throw .notAURL(trimmed) }
        self.origin = url
    }

    var host: String { origin.host() ?? "" }

    /// The origin without a trailing slash, as typed back into a form.
    var description: String { origin.absoluteString }

    /// The server URL of an app-relative target (`/path?query`).
    func url(forTarget target: String) -> URL? {
        URL(string: origin.absoluteString + target)
    }

    /// Whether `url` points at this server (same scheme, host and port).
    func owns(_ url: URL) -> Bool {
        guard let other = try? ServerAddress(parsing: Self.originString(of: url)) else { return false }
        return other == self
    }

    private static func originString(of url: URL) -> String {
        var components = URLComponents()
        components.scheme = url.scheme
        components.host = url.host()
        components.port = url.port
        return components.url?.absoluteString ?? ""
    }

    /// Addresses App Transport Security's `NSAllowsLocalNetworking` lets through over plain
    /// http: loopback, unqualified names, `.local` names and private IP addresses.
    static func isLocal(host: String) -> Bool {
        let host = host.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        if host == "localhost" || host == "::1" || host.hasSuffix(".local") { return true }
        if !host.contains(".") && !host.contains(":") { return true } // unqualified name
        let octets = host.split(separator: ".").compactMap { UInt8($0) }
        if octets.count == 4 {
            switch (octets[0], octets[1]) {
            case (127, _), (10, _), (192, 168), (169, 254): return true
            case (172, 16...31): return true
            default: return false
            }
        }
        guard host.contains(":") else { return false }
        return host.hasPrefix("fe80:") || host.hasPrefix("fd") || host.hasPrefix("fc")
    }

    // Stored as the origin string and validated again when read back.
    init(from decoder: any Decoder) throws {
        let text = try decoder.singleValueContainer().decode(String.self)
        do {
            try self.init(parsing: text)
        } catch {
            throw DecodingError.dataCorrupted(
                .init(codingPath: decoder.codingPath, debugDescription: error.localizedDescription)
            )
        }
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(description)
    }
}

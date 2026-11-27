import Foundation

/// The app's own links (`airistotle://…`), used by the widget, the reminders, the Shortcuts and
/// Spotlight alike:
///
///     airistotle://review                   the native review of everything due
///     airistotle://review/quick             a quick (timed) review
///     airistotle://continue                 the page the server says to resume
///     airistotle://page/<slug>              a study page
///     airistotle://add?url=<http(s) address> the Add page with that address filled in (it is
///                                           not fetched or added until the reader says so)
///
/// Anything else is refused with a message saying so. No link makes the web view load an
/// address of its own choosing: every destination is one of the server's own pages.
enum DeepLink: Equatable, Sendable {
    case review(quick: Bool)
    case continueStudying
    case page(slug: String)
    case add(URL)

    static let scheme = "airistotle"

    struct Refused: Error, Equatable, LocalizedError {
        var message: String
        var errorDescription: String? { message }
    }

    var url: URL {
        var parts = URLComponents()
        parts.scheme = Self.scheme
        switch self {
        case .review(let quick):
            parts.host = "review"
            parts.path = quick ? "/quick" : ""
        case .continueStudying:
            parts.host = "continue"
        case .page(let slug):
            parts.host = "page"
            parts.percentEncodedPath = "/" + (slug.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? slug)
        case .add(let address):
            parts.host = "add"
            parts.queryItems = [URLQueryItem(name: "url", value: address.absoluteString)]
        }
        // Every part above is valid, so the URL can be made.
        return parts.url ?? URL(fileURLWithPath: "/")
    }

    /// A study page's slug as the server makes them: letters, digits, `-`, `_` and `.`.
    static func isValidSlug(_ slug: String) -> Bool {
        guard !slug.isEmpty, slug.count <= 200, slug != ".", slug != ".." else { return false }
        return slug.unicodeScalars.allSatisfy { scalar in
            (scalar.isASCII && CharacterSet.alphanumerics.contains(scalar)) || "-_.".unicodeScalars.contains(scalar)
        }
    }

    /// An address the Add page may be given: http or https with a host, nothing else.
    static func isAddableAddress(_ url: URL) -> Bool {
        guard let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https" else { return false }
        guard let host = url.host(percentEncoded: false), !host.isEmpty else { return false }
        return url.user == nil && url.password == nil
    }

    static func parse(_ url: URL) throws(Refused) -> DeepLink {
        let shown = url.absoluteString.count > 120 ? String(url.absoluteString.prefix(120)) + "…" : url.absoluteString
        func refuse(_ why: String) -> Refused { Refused(message: "Airistotle cannot open the link \(shown): \(why)") }
        guard url.scheme?.lowercased() == scheme else { throw refuse("it is not an Airistotle link.") }
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { throw refuse("it is malformed.") }
        guard parts.user == nil, parts.password == nil, parts.port == nil, parts.fragment == nil else {
            throw refuse("it has parts Airistotle links never have.")
        }
        let query = parts.queryItems ?? []
        let path = parts.percentEncodedPath
        switch parts.host?.lowercased() {
        case "review":
            guard query.isEmpty else { throw refuse("a review link takes no options.") }
            switch path {
            case "", "/": return .review(quick: false)
            case "/quick": return .review(quick: true)
            default: throw refuse("there is no review called \(path.dropFirst()).")
            }
        case "continue":
            guard query.isEmpty, path.isEmpty || path == "/" else { throw refuse("a continue link takes nothing more.") }
            return .continueStudying
        case "page":
            guard query.isEmpty else { throw refuse("a page link takes no options.") }
            let segments = path.split(separator: "/", omittingEmptySubsequences: false)
            guard segments.count == 2, segments[0].isEmpty, let slug = String(segments[1]).removingPercentEncoding,
                  isValidSlug(slug) else { throw refuse("it does not name a study page.") }
            return .page(slug: slug)
        case "add":
            guard path.isEmpty || path == "/" else { throw refuse("an add link takes only an address.") }
            guard query.count == 1, query[0].name == "url", let value = query[0].value?.trimmingCharacters(in: .whitespaces),
                  let address = URL(string: value), isAddableAddress(address) else {
                throw refuse("an add link needs one web address starting with http:// or https://.")
            }
            return .add(address)
        case let other?:
            throw refuse("Airistotle has nothing called “\(other)”.")
        case nil:
            throw refuse("it names no destination.")
        }
    }
}

import Foundation

/// Where a navigation in the web view goes. The web view only ever shows the app's own
/// `teacher://app/` pages; other addresses open outside the app.
enum LinkPolicy {
    enum Decision: Equatable {
        /// Let the web view load it.
        case allow
        /// Cancel it, then open `url` in the web view instead (a link to the server's own
        /// https address, mapped to the app's origin so it goes through the gateway).
        case openInApp(URL)
        /// Cancel it and hand `url` to the system (Safari, Mail, ...).
        case openOutside(URL)
        /// Cancel it.
        case cancel
    }

    static func decide(_ url: URL?, isMainFrame: Bool, server: ServerAddress) -> Decision {
        guard let url, let scheme = url.scheme?.lowercased() else { return .cancel }
        switch scheme {
        case AppURL.scheme:
            return AppURL.target(of: url) != nil ? .allow : .cancel
        case "about", "blob", "data", "javascript":
            // about:blank and about:srcdoc frames (the sandboxed interactables and lab runner).
            return .allow
        case "http", "https":
            if let appURL = AppURL.appURL(forServerURL: url, server: server) {
                return isMainFrame ? .openInApp(appURL) : .allow
            }
            return isMainFrame ? .openOutside(url) : .allow
        default:
            // mailto:, tel: and the like.
            return isMainFrame ? .openOutside(url) : .cancel
        }
    }
}

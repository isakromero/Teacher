import Foundation

/// The page that was on screen, and how far down it was scrolled, for one server and account.
struct LastPage: Codable, Sendable, Equatable {
    /// An app target (`/artifacts/x?full=1`).
    var target: String
    /// The page's `window.scrollY` (CSS pixels) when it was last reported.
    var scrollY: Double

    /// Whether `target` is worth reopening at the next launch: an app target that is not one
    /// of the sign-in pages the app replaces, and not an API answer or a static file.
    static func isRememberable(_ target: String) -> Bool {
        guard target.hasPrefix("/"), !target.hasPrefix("//"), AppURL.url(forTarget: target) != nil else { return false }
        let path = String(target.prefix { $0 != "?" && $0 != "#" })
        if AuthPagesRoute.signInPaths.contains(path) || path == AuthPagesRoute.signOutPath { return false }
        return !path.hasPrefix("/api/") && !path.hasPrefix("/static/")
    }
}

/// Where the last page of each server and account is kept: UserDefaults, one key per cache
/// namespace (a hash of the server and the account, so no address or id is in the key). It
/// holds a path and a number, nothing secret.
struct LastPageStore: @unchecked Sendable {
    let defaults: UserDefaults
    static let keyPrefix = "lastPage."

    private func key(_ namespace: String) -> String { Self.keyPrefix + namespace }

    /// The remembered page, or nil when there is none. A stored value that cannot be read is
    /// removed and reported (it is never silently treated as a page).
    func page(for namespace: String) -> LastPage? {
        guard let data = defaults.data(forKey: key(namespace)) else { return nil }
        do {
            let page = try JSONDecoder().decode(LastPage.self, from: data)
            guard LastPage.isRememberable(page.target) else {
                throw DecodingError.dataCorrupted(.init(codingPath: [], debugDescription: "not a page target: \(page.target)"))
            }
            return page
        } catch {
            defaults.removeObject(forKey: key(namespace))
            print("Airistotle: the remembered last page could not be read and was removed: \(error)")
            return nil
        }
    }

    /// Remember `target` as the page on screen. The same target keeps its scroll position (a
    /// reload, or the URL settling after the launch load); a new one starts at the top.
    func remember(target: String, for namespace: String) {
        guard LastPage.isRememberable(target) else { return }
        if page(for: namespace)?.target == target { return }
        save(LastPage(target: target, scrollY: 0), for: namespace)
    }

    /// Remember how far `target` is scrolled. Ignored unless `target` is the remembered page
    /// (a late report from the page that was just left).
    func remember(scrollY: Double, of target: String, for namespace: String) {
        guard scrollY.isFinite, var page = page(for: namespace), page.target == target else { return }
        page.scrollY = max(0, scrollY)
        save(page, for: namespace)
    }

    func forget(_ namespace: String) {
        defaults.removeObject(forKey: key(namespace))
    }

    /// Forget every server's and account's last page (the UI tests' clean start).
    func forgetAll() {
        for key in defaults.dictionaryRepresentation().keys where key.hasPrefix(Self.keyPrefix) {
            defaults.removeObject(forKey: key)
        }
    }

    private func save(_ page: LastPage, for namespace: String) {
        do {
            defaults.set(try JSONEncoder().encode(page), forKey: key(namespace))
        } catch {
            print("Airistotle: the last page could not be remembered: \(error)")
        }
    }
}

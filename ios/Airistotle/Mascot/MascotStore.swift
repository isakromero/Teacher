import Foundation

/// What Aristotle remembers (UserDefaults; nothing secret): whether he is shown, when the app was
/// last active, and which pages he has congratulated the reader on, per server and account (one
/// key per cache namespace, as `LastPageStore`). Signing out keeps all of it, so signing in
/// again does not repeat a congratulation.
struct MascotStore: @unchecked Sendable {
    let defaults: UserDefaults

    static let enabledKey = "mascotShown"
    static let lastActiveKey = "mascotLastActive"
    static let congratulatedPrefix = "mascotCongratulated."
    static var allKeys: [String] { [enabledKey, lastActiveKey] }

    /// A stored value that cannot be read. It is left as it is (not replaced), and said.
    struct Unreadable: Error, LocalizedError, Equatable {
        var what: String
        var errorDescription: String? {
            "Aristotle's memory of \(what) on this phone cannot be read, so he says nothing about it."
        }
    }

    /// "Show Aristotle" (default on).
    var enabled: Bool {
        get { defaults.object(forKey: Self.enabledKey) as? Bool ?? true }
        nonmutating set { defaults.set(newValue, forKey: Self.enabledKey) }
    }

    /// When the app was last active (nil: never, as on the first launch).
    func lastActive() throws(Unreadable) -> Date? {
        guard let stored = defaults.object(forKey: Self.lastActiveKey) else { return nil }
        guard let seconds = stored as? Double, seconds.isFinite else { throw Unreadable(what: "when the app was last open") }
        return Date(timeIntervalSince1970: seconds)
    }

    func setLastActive(_ date: Date) {
        defaults.set(date.timeIntervalSince1970, forKey: Self.lastActiveKey)
    }

    private func key(_ namespace: String) -> String { Self.congratulatedPrefix + namespace }

    /// The pages congratulated on in `namespace`.
    func congratulated(_ namespace: String) throws(Unreadable) -> Set<String> {
        guard let stored = defaults.object(forKey: key(namespace)) else { return [] }
        guard let data = stored as? Data, let slugs = try? JSONDecoder().decode([String].self, from: data) else {
            throw Unreadable(what: "the pages you finished")
        }
        return Set(slugs)
    }

    func setCongratulated(_ slugs: Set<String>, namespace: String) throws {
        defaults.set(try JSONEncoder().encode(slugs.sorted()), forKey: key(namespace))
    }

    /// Forget the pages congratulated in `namespace` (only when they cannot be read: Settings).
    func forgetCongratulated(_ namespace: String) {
        defaults.removeObject(forKey: key(namespace))
    }

    /// Forget everything (the UI tests' clean start; never on signing out).
    func forgetAll() {
        for key in Self.allKeys { defaults.removeObject(forKey: key) }
        for key in defaults.dictionaryRepresentation().keys where key.hasPrefix(Self.congratulatedPrefix) {
            defaults.removeObject(forKey: key)
        }
    }
}

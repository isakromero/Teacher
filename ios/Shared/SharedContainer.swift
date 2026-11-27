import Foundation

/// The App Group container the app shares with its widget: the one place the group is named.
/// It holds only the widget's snapshot (`WidgetSnapshot`); the saved pages, the review questions
/// and the session stay in the app's own container.
///
/// The group needs the App Groups entitlement in both the app and the widget, and an App Group
/// registered for the team. Without it (a signing problem) `live()` says so, and the widget and
/// the app's Settings show that the widget cannot get its data; nothing falls back silently.
struct SharedContainer: Sendable {
    static let groupIdentifier = "group.dev.airistotle.reader"
    static let snapshotName = "widget-snapshot.json"

    enum Failure: Error, Equatable, LocalizedError {
        /// The App Group container cannot be opened (entitlement or provisioning missing).
        case unavailable(String)
        case unreadable(String)
        case unwritable(String)

        var errorDescription: String? {
            switch self {
            case .unavailable(let reason): "The data shared with the widget cannot be opened: \(reason)"
            case .unreadable(let reason): "The widget's data cannot be read: \(reason)"
            case .unwritable(let reason): "The widget's data cannot be written: \(reason)"
            }
        }
    }

    let root: URL

    init(root: URL) {
        self.root = root
    }

    /// The group's container on this device.
    static func live() -> Result<SharedContainer, Failure> {
        guard let url = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: groupIdentifier) else {
            return .failure(.unavailable("the App Group \(groupIdentifier) is not available to this build "
                + "(its App Groups entitlement or provisioning is missing)."))
        }
        return .success(SharedContainer(root: url))
    }

    var snapshotURL: URL { root.appending(path: Self.snapshotName) }

    /// The snapshot; nil when the app has not written one yet.
    func readSnapshot() throws(Failure) -> WidgetSnapshot? {
        let path = snapshotURL.path(percentEncoded: false)
        guard FileManager.default.fileExists(atPath: path) else { return nil }
        let data: Data
        do {
            data = try Data(contentsOf: snapshotURL)
        } catch {
            throw .unreadable(error.localizedDescription)
        }
        do {
            return try WidgetSnapshot.decode(data)
        } catch {
            throw .unreadable(error.message)
        }
    }

    /// `readSnapshot()` as a result.
    var snapshotRead: Result<WidgetSnapshot?, Failure> {
        do {
            return .success(try readSnapshot())
        } catch {
            return .failure(error)
        }
    }

    /// The snapshot from the group's container, or why it cannot be read.
    static func readLive() -> Result<WidgetSnapshot?, Failure> {
        live().flatMap(\.snapshotRead)
    }

    func writeSnapshot(_ snapshot: WidgetSnapshot) throws(Failure) {
        do {
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
            // Readable while the phone is locked (lock screen widgets), after the first unlock.
            try snapshot.encoded().write(to: snapshotURL, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        } catch {
            throw .unwritable(error.localizedDescription)
        }
    }

    func removeSnapshot() throws(Failure) {
        let path = snapshotURL.path(percentEncoded: false)
        guard FileManager.default.fileExists(atPath: path) else { return }
        do {
            try FileManager.default.removeItem(at: snapshotURL)
        } catch {
            throw .unwritable(error.localizedDescription)
        }
    }
}

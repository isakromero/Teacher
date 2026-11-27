import Foundation
import Network
import Synchronization

/// What the network allows now, from `NWPathMonitor`.
struct NetworkConditions: Sendable, Equatable {
    var satisfied: Bool
    /// A cellular or personal-hotspot path (`NWPath.isExpensive`).
    var expensive: Bool
    /// Low Data Mode (`NWPath.isConstrained`).
    var constrained: Bool

    static let unknown = NetworkConditions(satisfied: false, expensive: true, constrained: false)
}

/// The phone's network path, kept up to date by `NWPathMonitor`.
final class NetworkWatcher: Sendable {
    private final class State: Sendable {
        let value = Mutex<NetworkConditions>(.unknown)
    }

    private let monitor = NWPathMonitor()
    private let state = State()

    init() {
        let state = state
        monitor.pathUpdateHandler = { path in
            state.value.withLock {
                $0 = NetworkConditions(satisfied: path.status == .satisfied, expensive: path.isExpensive,
                                       constrained: path.isConstrained)
            }
        }
        monitor.start(queue: DispatchQueue(label: "airistotle.network"))
    }

    deinit { monitor.cancel() }

    var conditions: NetworkConditions { state.value.withLock { $0 } }
}

/// Whether automatic saving may run now, and if not, why (shown in the Saved list).
enum AutoSaveGate: Equatable {
    case allowed
    case off
    case lowDataMode
    case waitingForWiFi
    case noNetwork

    static func decide(enabled: Bool, wifiOnly: Bool, network: NetworkConditions) -> AutoSaveGate {
        guard enabled else { return .off }
        guard network.satisfied else { return .noNetwork }
        if network.constrained { return .lowDataMode }
        if wifiOnly && network.expensive { return .waitingForWiFi }
        return .allowed
    }

    var status: String? {
        switch self {
        case .allowed, .off: nil
        case .lowDataMode: "Automatic saving is paused while Low Data Mode is on."
        case .waitingForWiFi: "Automatic saving waits for Wi-Fi (Settings: Only on Wi-Fi)."
        case .noNetwork: "Automatic saving waits for a network connection."
        }
    }
}

/// A page the reader is studying and has not finished: the one on screen, and those
/// `GET /api/history/continue` lists with `progress.complete` false.
struct AutoSaveCandidate: Sendable, Equatable {
    var slug: String
    var title: String

    /// The study pages of a `/api/history/continue` answer that are ready and not finished
    /// (labs have no offline bundle); `complete` lists the finished ones.
    static func fromContinue(_ value: JSONValue) -> (inProgress: [AutoSaveCandidate], complete: Set<String>) {
        var out: [AutoSaveCandidate] = []
        var complete = Set<String>()
        for item in value["items"].array ?? [] {
            let artifact = item["artifact"]
            guard let slug = artifact["slug"].string, artifact["kind"].string != "lab",
                  item["url"].string.flatMap(OfflineService.pageSlug) == slug, artifact["status"].string == "ready" else { continue }
            if artifact["progress"]["complete"].bool == true {
                complete.insert(slug)
            } else {
                out.append(AutoSaveCandidate(slug: slug, title: artifact["title"].string ?? slug))
            }
        }
        return (out, complete)
    }
}

/// Which pages to save automatically and which automatic saves to evict. Pages saved by hand
/// are never saved over, evicted or counted; pages with work waiting to upload are never evicted.
struct AutoSavePolicy: Sendable, Equatable {
    var cap = 5
    /// An automatic save younger than this is fresh enough not to be fetched again.
    var refreshAfter: TimeInterval = 6 * 3600

    struct Saved: Sendable, Equatable {
        var slug: String
        var automatic: Bool
        var savedAt: Date
        var hasPendingWork: Bool
    }

    /// The candidates to fetch now, most recent first: at most `cap`, skipping pages saved by
    /// hand and fresh automatic saves.
    func toSave(_ candidates: [AutoSaveCandidate], saved: [Saved], now: Date) -> [AutoSaveCandidate] {
        var seen = Set<String>()
        let unique = candidates.filter { seen.insert($0.slug).inserted }
        let bySlug = Dictionary(saved.map { ($0.slug, $0) }) { first, _ in first }
        return Array(unique.filter { bySlug[$0.slug]?.automatic != false }.prefix(cap)).filter { candidate in
            guard let existing = bySlug[candidate.slug] else { return true }
            return now.timeIntervalSince(existing.savedAt) >= refreshAfter && !existing.hasPendingWork
        }
    }

    /// The automatic saves to remove so that at most `cap` remain: the oldest first, never one
    /// with work waiting to upload.
    func toEvict(_ saved: [Saved]) -> [String] {
        let automatic = saved.filter(\.automatic).sorted { $0.savedAt > $1.savedAt }
        guard automatic.count > cap else { return [] }
        let excess = automatic.count - cap
        return Array(automatic.reversed().filter { !$0.hasPendingWork }.prefix(excess).map(\.slug))
    }
}

/// A failed automatic save, for the Saved list (never an alert while reading).
struct AutoSaveNote: Codable, Sendable, Equatable, Identifiable {
    var id: UUID
    var date: Date
    var slug: String
    var title: String
    var message: String
}

/// Saves the pages the reader is studying (through `OfflineService.save`, the same path as
/// "Save for offline") when the settings and the network allow it; failures become notes.
final class AutoSaver: Sendable {
    let service: OfflineService
    let preferences: OfflinePreferences
    let policy: AutoSavePolicy
    private let network: @Sendable () -> NetworkConditions
    private let running = Mutex(false)

    init(service: OfflineService, preferences: OfflinePreferences, policy: AutoSavePolicy = AutoSavePolicy(),
         network: @escaping @Sendable () -> NetworkConditions) {
        self.service = service
        self.preferences = preferences
        self.policy = policy
        self.network = network
    }

    var gate: AutoSaveGate {
        AutoSaveGate.decide(enabled: preferences.autoSave, wifiOnly: preferences.autoSaveWiFiOnly, network: network())
    }

    /// One pass: find the pages in progress (`current` is the study page on screen), save
    /// those that need it, evict the oldest automatic saves over the cap. Nil when it did not
    /// run (another pass is under way); else the gate it ran under.
    @discardableResult
    func run(current: String?) async -> AutoSaveGate? {
        let gate = gate
        guard gate == .allowed, let namespace = service.namespace else { return gate }
        guard running.withLock({ busy in
            if busy { return false }
            busy = true
            return true
        }) else { return nil }
        defer { running.withLock { $0 = false } }

        var candidates: [AutoSaveCandidate] = []
        var complete = Set<String>()
        do {
            let answer = try await service.gateway.fetch("GET", "/api/history/continue?limit=\(policy.cap)")
            guard answer.status == 200, let value = try? JSONValue(data: answer.body) else {
                let detail = AuthClient.detail(of: answer.body) ?? "HTTP \(answer.status)"
                await note(namespace, slug: "", title: "Pages you are studying",
                           "The app could not ask the server which pages you are studying (\(detail)), so none was saved.")
                return gate
            }
            (candidates, complete) = AutoSaveCandidate.fromContinue(value)
        } catch .unreachable {
            return gate // offline: nothing to fetch, and nothing failed
        } catch {
            await note(namespace, slug: "", title: "Pages you are studying",
                       "The app could not ask the server which pages you are studying: \(error.localizedDescription)")
            return gate
        }
        if let current, !complete.contains(current), !candidates.contains(where: { $0.slug == current }) {
            candidates.insert(AutoSaveCandidate(slug: current, title: current), at: 0)
        }
        let saved = await savedState(namespace)
        for candidate in policy.toSave(candidates, saved: saved, now: Date()) {
            guard self.gate == .allowed else { break } // the network changed meanwhile
            do {
                _ = try await service.save(slug: candidate.slug, automatic: true)
                try? await service.store.removeAutoSaveNotes(namespace, slug: candidate.slug)
            } catch {
                await note(namespace, slug: candidate.slug, title: candidate.title, error.localizedDescription)
            }
        }
        for slug in policy.toEvict(await savedState(namespace)) {
            do {
                try await service.remove(slug: slug)
            } catch {
                await note(namespace, slug: slug, title: slug, "The oldest automatic save could not be removed to make room: "
                    + error.localizedDescription)
            }
        }
        service.changed()
        return gate
    }

    private func savedState(_ namespace: String) async -> [AutoSavePolicy.Saved] {
        await service.store.pages(namespace).map {
            AutoSavePolicy.Saved(slug: $0.record.slug, automatic: $0.record.automatic, savedAt: $0.record.savedAt,
                                 hasPendingWork: !$0.pending.isEmpty || !$0.highlights.changes.isEmpty)
        }
    }

    private func note(_ namespace: String, slug: String, title: String, _ message: String) async {
        let entry = AutoSaveNote(id: UUID(), date: Date(), slug: slug, title: title, message: message)
        do {
            try await service.store.appendAutoSaveNote(namespace, entry)
        } catch {
            print("Airistotle: an automatic-save failure could not be noted (\(error)): \(message)")
        }
        service.changed()
    }
}

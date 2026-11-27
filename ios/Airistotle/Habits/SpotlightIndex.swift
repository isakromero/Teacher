import CoreSpotlight
import Foundation
import UniformTypeIdentifiers

/// A saved study page as Spotlight lists it.
struct SpotlightEntry: Codable, Equatable, Sendable {
    var id: String
    var title: String
    var detail: String

    static let domain = "dev.airistotle.saved-pages"

    /// "page|<namespace>|<slug>": the server and account it was saved under, and the page.
    static func identifier(namespace: String, slug: String) -> String { "page|\(namespace)|\(slug)" }

    static func parse(_ identifier: String) -> (namespace: String, slug: String)? {
        let parts = identifier.split(separator: "|", maxSplits: 2, omittingEmptySubsequences: false)
        guard parts.count == 3, parts[0] == "page", !parts[1].isEmpty, !parts[2].isEmpty else { return nil }
        return (String(parts[1]), String(parts[2]))
    }

    init(id: String, title: String, detail: String) {
        (self.id, self.title, self.detail) = (id, title, detail)
    }

    init(namespace: String, page: SavedPageSummary) {
        id = Self.identifier(namespace: namespace, slug: page.slug)
        title = page.title
        let progress = page.gatesTotal > 0 ? " \(page.gatesCleared) of \(page.gatesTotal) checkpoints cleared." : ""
        detail = "Study page saved for offline reading in Airistotle.\(progress)"
    }
}

/// What to change in the index so it lists exactly the saved pages.
enum SpotlightPlan {
    static func diff(indexed: [String: SpotlightEntry], wanted: [SpotlightEntry]) -> (upsert: [SpotlightEntry], remove: [String]) {
        let wantedIDs = Set(wanted.map(\.id))
        let upsert = wanted.filter { indexed[$0.id] != $0 }
        let remove = indexed.keys.filter { !wantedIDs.contains($0) }.sorted()
        return (upsert, remove)
    }
}

protocol SpotlightIndexing: Sendable {
    func index(_ entries: [SpotlightEntry]) async throws
    func remove(_ ids: [String]) async throws
    func removeAll() async throws
}

/// The phone's Core Spotlight index.
struct SystemSpotlightIndex: SpotlightIndexing {
    func index(_ entries: [SpotlightEntry]) async throws {
        let items = entries.map { entry in
            let attributes = CSSearchableItemAttributeSet(contentType: .text)
            attributes.title = entry.title
            attributes.contentDescription = entry.detail
            attributes.displayName = entry.title
            return CSSearchableItem(uniqueIdentifier: entry.id, domainIdentifier: SpotlightEntry.domain, attributeSet: attributes)
        }
        try await CSSearchableIndex.default().indexSearchableItems(items)
    }

    func remove(_ ids: [String]) async throws {
        try await CSSearchableIndex.default().deleteSearchableItems(withIdentifiers: ids)
    }

    func removeAll() async throws {
        try await CSSearchableIndex.default().deleteSearchableItems(withDomainIdentifiers: [SpotlightEntry.domain])
    }
}

/// Keeps the index in step with the saved pages of the account signed in, and remembers what
/// it put there (so a removed page leaves the index too).
@MainActor
final class SpotlightBook {
    private let index: any SpotlightIndexing
    private let defaults: UserDefaults
    private(set) var problem: String?

    init(index: any SpotlightIndexing, defaults: UserDefaults) {
        self.index = index
        self.defaults = defaults
    }

    var indexed: [String: SpotlightEntry] {
        guard let data = defaults.data(forKey: HabitPreferences.spotlightKey) else { return [:] }
        do {
            return try JSONDecoder().decode([String: SpotlightEntry].self, from: data)
        } catch {
            problem = "The record of what is in Spotlight cannot be read (\(error.localizedDescription)); it is rebuilt."
            return [:]
        }
    }

    private func record(_ entries: [String: SpotlightEntry]) throws {
        defaults.set(try JSONEncoder().encode(entries), forKey: HabitPreferences.spotlightKey)
    }

    /// List exactly `pages` (of `namespace`).
    func update(namespace: String, pages: [SavedPageSummary]) async {
        let before = indexed
        let wanted = pages.map { SpotlightEntry(namespace: namespace, page: $0) }
        let (upsert, remove) = SpotlightPlan.diff(indexed: before, wanted: wanted)
        guard !upsert.isEmpty || !remove.isEmpty else { return }
        do {
            if !remove.isEmpty { try await index.remove(remove) }
            if !upsert.isEmpty { try await index.index(upsert) }
            try record(Dictionary(wanted.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first }))
            problem = nil
        } catch {
            problem = "Spotlight could not be updated with the saved pages: \(error.localizedDescription)"
        }
    }

    /// Signing out: nothing of this account stays searchable.
    func removeAll() async {
        do {
            try await index.removeAll()
            try record([:])
            problem = nil
        } catch {
            problem = "The saved pages could not be removed from Spotlight: \(error.localizedDescription)"
        }
    }
}

import Foundation
import Observation

/// A saved page as the Saved list shows it.
struct SavedPageSummary: Identifiable, Equatable, Sendable {
    var slug: String
    var title: String
    var savedAt: Date
    var bytes: Int64
    var gatesCleared: Int
    var gatesTotal: Int
    var pending: Int
    /// The server has another version of the page (it was regenerated).
    var regenerated: Bool
    var supersededBy: String?
    var deletedOnServer: Bool

    var id: String { slug }

    init(_ page: SavedPage) {
        let engine = page.engine
        slug = page.record.slug
        title = page.record.title
        savedAt = page.record.savedAt
        bytes = page.record.bytes
        gatesCleared = engine.gatesCleared
        gatesTotal = engine.gatesTotal
        pending = page.pending.count
        regenerated = page.record.serverVersion != nil
        supersededBy = page.record.supersededBy
        deletedOnServer = page.record.deletedOnServer
    }
}

/// The offline state the screens show, kept up to date from `OfflineService`.
@MainActor
@Observable
final class OfflineCenter {
    private(set) var pages: [SavedPageSummary] = []
    private(set) var log: [SyncLogEntry] = []
    /// Pages being saved, with how far along (0...1).
    private(set) var saving: [String: Double] = [:]
    private(set) var syncing = false
    private(set) var lastSync: OfflineService.SyncSummary?
    private(set) var modelAvailability: JudgeAvailability = .unavailable("Not checked yet")

    @ObservationIgnored let service: OfflineService
    @ObservationIgnored private var updates: Task<Void, Never>?

    init(service: OfflineService, changes: AsyncStream<Void>) {
        self.service = service
        updates = Task { [weak self] in
            for await _ in changes { await self?.reload() }
        }
    }

    var pendingCount: Int { pages.reduce(0) { $0 + $1.pending } }
    var totalBytes: Int64 { pages.reduce(0) { $0 + $1.bytes } }

    var grading: WrittenAnswerGrading {
        get {
            access(keyPath: \.grading)
            return service.preferences.grading
        }
        set {
            withMutation(keyPath: \.grading) { service.preferences.grading = newValue }
        }
    }

    func isSaved(_ slug: String) -> Bool { pages.contains { $0.slug == slug } }
    func summary(_ slug: String) -> SavedPageSummary? { pages.first { $0.slug == slug } }

    func reload() async {
        modelAvailability = service.grader.judge.availability()
        guard let namespace = service.namespace else {
            pages = []
            log = []
            return
        }
        pages = await service.store.pages(namespace).map(SavedPageSummary.init)
        log = await service.store.log(namespace).reversed()
    }

    /// Save (or refresh) a page; throws with what failed.
    func save(_ slug: String) async throws {
        saving[slug] = 0
        defer { saving[slug] = nil }
        _ = try await service.save(slug: slug) { [weak self] done, total in
            Task { @MainActor [weak self] in
                guard self?.saving[slug] != nil else { return }
                self?.saving[slug] = total > 0 ? Double(done) / Double(total) : 0
            }
        }
        await reload()
    }

    func remove(_ slug: String) async throws {
        try await service.remove(slug: slug)
        await reload()
    }

    func syncNow() async {
        guard !syncing else { return }
        syncing = true
        defer { syncing = false }
        lastSync = await service.sync()
        await reload()
    }

    func dismiss(_ ids: Set<UUID>?) async {
        guard let namespace = service.namespace else { return }
        try? await service.store.dismissLog(namespace, ids: ids)
        await reload()
    }
}

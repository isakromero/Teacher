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
    /// Highlight and note changes made offline that wait to upload.
    var highlightChanges: Int
    /// Saved automatically (the reader is studying it), not by hand.
    var automatic: Bool
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
        highlightChanges = page.highlights.changes.count
        automatic = page.record.automatic
        regenerated = page.record.serverVersion != nil
        supersededBy = page.record.supersededBy
        deletedOnServer = page.record.deletedOnServer
    }
}

/// The offline state the screens show, kept up to date from `OfflineService` (saved pages,
/// their answers and highlights), `ReviewService` (the review queue) and `AutoSaver`.
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
    /// The review queue saved on the phone (nil: none saved yet).
    private(set) var reviewQueue: LocalReviewQueue?
    /// Review answers made offline that wait to upload.
    private(set) var reviewPending = 0
    /// Why the review queue could not be saved or read the last time (nil: it was).
    private(set) var reviewProblem: String?
    private(set) var autoSaveNotes: [AutoSaveNote] = []
    /// Why automatic saving is not running now, if it is on but cannot run.
    private(set) var autoSaveStatus: String?

    @ObservationIgnored let service: OfflineService
    @ObservationIgnored let review: ReviewService
    @ObservationIgnored let autoSaver: AutoSaver
    @ObservationIgnored private var updates: Task<Void, Never>?
    /// Told after every reload (the widget's snapshot and Spotlight follow the saved pages and
    /// the answers waiting), and after every sync.
    @ObservationIgnored var onReload: (@MainActor () -> Void)?
    @ObservationIgnored var onSynced: (@MainActor (OfflineService.SyncSummary) -> Void)?

    init(service: OfflineService, review: ReviewService, autoSaver: AutoSaver, changes: AsyncStream<Void>) {
        self.service = service
        self.review = review
        self.autoSaver = autoSaver
        updates = Task { [weak self] in
            for await _ in changes { await self?.reload() }
        }
    }

    /// Answers made offline that wait to upload: checkpoint answers and review answers.
    var pendingCount: Int { pages.reduce(0) { $0 + $1.pending } + reviewPending }
    var pendingHighlightChanges: Int { pages.reduce(0) { $0 + $1.highlightChanges } }
    /// Anything at all waiting to upload.
    var hasPendingWork: Bool { pendingCount + pendingHighlightChanges > 0 }
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

    var autoSave: Bool {
        get {
            access(keyPath: \.autoSave)
            return service.preferences.autoSave
        }
        set {
            withMutation(keyPath: \.autoSave) { service.preferences.autoSave = newValue }
            autoSaveStatus = autoSaver.gate.status
        }
    }

    var autoSaveWiFiOnly: Bool {
        get {
            access(keyPath: \.autoSaveWiFiOnly)
            return service.preferences.autoSaveWiFiOnly
        }
        set {
            withMutation(keyPath: \.autoSaveWiFiOnly) { service.preferences.autoSaveWiFiOnly = newValue }
            autoSaveStatus = autoSaver.gate.status
        }
    }

    var quickReviewMinutes: Int {
        get {
            access(keyPath: \.quickReviewMinutes)
            return service.preferences.quickReviewMinutes
        }
        set {
            withMutation(keyPath: \.quickReviewMinutes) { service.preferences.quickReviewMinutes = newValue }
        }
    }

    func isSaved(_ slug: String) -> Bool { pages.contains { $0.slug == slug } }
    func summary(_ slug: String) -> SavedPageSummary? { pages.first { $0.slug == slug } }

    /// Review questions that may be answered now from the saved queue.
    func reviewDue(now: Instant = .now()) -> Int { reviewQueue?.askable(now: now).count ?? 0 }

    func reload() async {
        modelAvailability = service.grader.judge.availability()
        autoSaveStatus = autoSaver.gate.status
        guard let namespace = service.namespace else {
            pages = []
            log = []
            reviewQueue = nil
            reviewPending = 0
            autoSaveNotes = []
            return
        }
        pages = await service.store.pages(namespace).map(SavedPageSummary.init)
        log = await service.store.log(namespace).reversed()
        autoSaveNotes = await service.store.autoSaveNotes(namespace).reversed()
        reviewPending = await review.pendingAnswers().count
        do {
            reviewQueue = try await review.localQueue()
        } catch {
            reviewQueue = nil
            reviewProblem = "The review questions saved on this phone cannot be read: \(error.localizedDescription)"
        }
        onReload?()
    }

    /// Save (or refresh) a page by hand; throws with what failed.
    func save(_ slug: String) async throws {
        saving[slug] = 0
        defer { saving[slug] = nil }
        _ = try await service.save(slug: slug) { [weak self] done, total in
            Task { @MainActor [weak self] in
                guard self?.saving[slug] != nil else { return }
                self?.saving[slug] = total > 0 ? Double(done) / Double(total) : 0
            }
        }
        if let namespace = service.namespace { try? await service.store.removeAutoSaveNotes(namespace, slug: slug) }
        await reload()
    }

    func remove(_ slug: String) async throws {
        try await service.remove(slug: slug)
        await reload()
    }

    /// Remove every saved page (the confirmation says what waiting work this loses).
    func removeAll() async throws {
        for page in pages { try await service.remove(slug: page.slug) }
        await reload()
    }

    /// Bytes each saved page takes on disk, and the review queue's.
    func diskUsage() async -> (pages: [String: Int64], review: Int64) {
        guard let namespace = service.namespace else { return ([:], 0) }
        var out: [String: Int64] = [:]
        for page in pages { out[page.slug] = await service.store.diskUsage(namespace, page.slug) }
        return (out, await review.store.diskUsage(namespace))
    }

    /// Upload everything that waits: checkpoint answers, highlight changes, review answers.
    func syncNow() async {
        guard !syncing else { return }
        syncing = true
        defer { syncing = false }
        var summary = await service.sync()
        if !summary.unreachable {
            let reviews = await review.sync()
            summary.uploaded += reviews.uploaded
            summary.logged += reviews.logged
            summary.unreachable = reviews.unreachable
        }
        lastSync = summary
        await reload()
        onSynced?(summary)
    }

    /// Fetch the review queue again for offline use; a failure is kept to show (an
    /// unreachable server is not one: the saved queue simply stays).
    func refreshReview() async {
        do {
            if let renderer = try await review.refreshBundle() {
                reviewProblem = "The review questions are saved, but the maths renderer could not be saved with them, so maths "
                    + "in them may not show offline: \(renderer.localizedDescription)"
            } else {
                reviewProblem = nil
            }
        } catch .unreachable, .notConnected {
            // Offline or signed out: the saved queue stays as it is.
        } catch {
            reviewProblem = "The review questions could not be saved for offline use: \(error.localizedDescription)"
        }
        await reload()
    }

    /// Save the pages being studied, if the settings and the network allow it.
    func autoSave(current: String?) async {
        let gate = await autoSaver.run(current: current)
        if let gate { autoSaveStatus = gate.status }
        await reload()
    }

    func dismissAutoSaveNotes() async {
        guard let namespace = service.namespace else { return }
        try? await service.store.removeAutoSaveNotes(namespace)
        await reload()
    }

    func dismiss(_ ids: Set<UUID>?) async {
        guard let namespace = service.namespace else { return }
        try? await service.store.dismissLog(namespace, ids: ids)
        await reload()
    }

    /// Signing out of `namespace`: its saved review queue goes (answers waiting stay).
    func forgetReview(namespace: String) async {
        do {
            try await review.store.forgetBundle(namespace)
        } catch {
            reviewProblem = "The saved review questions could not be removed: \(error.localizedDescription)"
        }
        await reload()
    }
}

import CryptoKit
import Foundation

/// One saved response of a saved page (the page itself, a script, a JSON answer, an asset).
struct SavedFile: Codable, Sendable, Equatable {
    var file: String
    var status: Int
    var headers: [String: String]
    var bytes: Int64
}

/// What is known about a saved page.
struct SavedPageRecord: Codable, Sendable, Equatable {
    var slug: String
    var title: String
    var savedAt: Date
    /// The `version` of the saved bundle.
    var version: String
    /// The server's version when it was last seen and differs from `version` (the page was
    /// regenerated: refresh it).
    var serverVersion: String?
    var supersededBy: String?
    /// The server answered 404 for the page: it was deleted there.
    var deletedOnServer: Bool
    var bytes: Int64
    /// Saved responses by target (`/static/js/api.js`, `/api/artifacts/x/glossary`, ...).
    var files: [String: SavedFile]
}

/// An attempt made on the phone and not yet uploaded.
struct PendingAttempt: Codable, Sendable, Equatable, Identifiable {
    var clientID: UUID
    var key: String
    var blockType: String
    /// The version of the bundle it was made on (sent with the upload).
    var bundleVersion: String
    var madeAt: Instant
    /// The response row as the phone recorded it (shown by the reading endpoints).
    var row: JSONValue
    /// The upload item (`OfflineAttemptIn`), fixed when the attempt is made: the same UUID is
    /// never sent with different content.
    var upload: JSONValue

    var id: UUID { clientID }
}

/// A line in the sync log: something the reader should know about an upload.
struct SyncLogEntry: Codable, Sendable, Equatable, Identifiable {
    enum Kind: String, Codable, Sendable {
        case rejected, mismatch, deleted, regenerated, failed
    }

    var id: UUID
    var date: Date
    var slug: String
    var title: String
    var kind: Kind
    var message: String
    /// The attempt concerned (its block and a summary of the answer), if any.
    var attempt: String?
}

/// A saved page with its bundle and the attempts made on it since.
struct SavedPage: Sendable, Equatable {
    var record: SavedPageRecord
    var bundle: OfflineBundle
    var pending: [PendingAttempt]

    /// The reading engine: the bundle plus the attempts made on the phone.
    var engine: ReadingEngine {
        ReadingEngine(bundle: bundle.adding(pending.map { ($0.key, $0.row) }))
    }
}

/// A page being saved: everything is written into a staging folder first and only moved into
/// place, in one rename, when all of it arrived and checked out.
struct StagedPage: Sendable {
    var folder: URL
    var bundle: OfflineBundle
    var files: [String: SavedFile]
}

/// Saved pages, one folder per server and account (the namespace the response cache uses),
/// in Application Support (iOS does not evict it; it is backed up, as the queued attempts are the
/// reader's own work). "Clear saved copies" does not touch it.
///
///     <root>/<namespace>/pages/<slug>/page.json      SavedPageRecord
///                                     bundle.json    the bundle as the server sent it
///                                     pending.json   [PendingAttempt], written before a reply
///                                     files/<sha>    the saved responses
///     <root>/<namespace>/synclog.json                [SyncLogEntry]
actor SavedStore {
    nonisolated let root: URL
    private var index: [String: [String: (slug: String, file: SavedFile)]] = [:] // namespace -> target -> page

    init(root: URL) {
        self.root = root
    }

    static func defaultRoot() throws -> URL {
        let support = try FileManager.default.url(
            for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true
        )
        return support.appending(path: "Airistotle/SavedPages", directoryHint: .isDirectory)
    }

    struct StoreError: Error, LocalizedError {
        var message: String
        var errorDescription: String? { message }
    }

    // MARK: Paths

    private func namespaceFolder(_ namespace: String) -> URL {
        root.appending(path: namespace, directoryHint: .isDirectory)
    }

    private func pagesFolder(_ namespace: String) -> URL {
        namespaceFolder(namespace).appending(path: "pages", directoryHint: .isDirectory)
    }

    static func folderName(for slug: String) -> String {
        let safe = slug.unicodeScalars.allSatisfy { CharacterSet.alphanumerics.contains($0) || $0 == "-" || $0 == "_" }
        if safe && !slug.isEmpty { return slug }
        return "x-" + SHA256.hash(data: Data(slug.utf8)).prefix(12).map { String(format: "%02x", $0) }.joined()
    }

    private func pageFolder(_ namespace: String, _ slug: String) -> URL {
        pagesFolder(namespace).appending(path: Self.folderName(for: slug), directoryHint: .isDirectory)
    }

    static func fileName(for target: String) -> String {
        SHA256.hash(data: Data(target.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    /// A new, empty staging folder for a page being saved.
    func newStagingFolder() throws -> URL {
        let folder = root.appending(path: ".staging/\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: folder.appending(path: "files"), withIntermediateDirectories: true)
        return folder
    }

    nonisolated func discardStaging(_ folder: URL) {
        try? FileManager.default.removeItem(at: folder)
    }

    // MARK: Reading

    func pages(_ namespace: String) -> [SavedPage] {
        let folder = pagesFolder(namespace)
        let names = (try? FileManager.default.contentsOfDirectory(atPath: folder.path(percentEncoded: false))) ?? []
        return names.sorted().compactMap { try? load(folder.appending(path: $0, directoryHint: .isDirectory)) }
            .sorted { $0.record.savedAt > $1.record.savedAt }
    }

    func page(_ namespace: String, _ slug: String) -> SavedPage? {
        try? load(pageFolder(namespace, slug))
    }

    func isSaved(_ namespace: String, _ slug: String) -> Bool {
        FileManager.default.fileExists(atPath: pageFolder(namespace, slug).appending(path: "page.json").path(percentEncoded: false))
    }

    private func load(_ folder: URL) throws -> SavedPage {
        let record = try Self.decoder.decode(SavedPageRecord.self, from: Data(contentsOf: folder.appending(path: "page.json")))
        let bundle = try OfflineBundle(data: Data(contentsOf: folder.appending(path: "bundle.json")))
        let pendingURL = folder.appending(path: "pending.json")
        let pending = FileManager.default.fileExists(atPath: pendingURL.path(percentEncoded: false))
            ? try Self.decoder.decode([PendingAttempt].self, from: Data(contentsOf: pendingURL)) : []
        return SavedPage(record: record, bundle: bundle, pending: pending)
    }

    /// A saved response for `target` from any saved page of the namespace, and its file.
    func file(_ namespace: String, target: String) -> (SavedFile, URL)? {
        if index[namespace] == nil { rebuildIndex(namespace) }
        guard let entry = index[namespace]?[target] else { return nil }
        let url = pageFolder(namespace, entry.slug).appending(path: "files/\(entry.file.file)")
        guard FileManager.default.fileExists(atPath: url.path(percentEncoded: false)) else { return nil }
        return (entry.file, url)
    }

    private func rebuildIndex(_ namespace: String) {
        var map: [String: (slug: String, file: SavedFile)] = [:]
        for page in pages(namespace) {
            for (target, file) in page.record.files where map[target] == nil {
                map[target] = (page.record.slug, file)
            }
        }
        index[namespace] = map
    }

    func totalBytes(_ namespace: String) -> Int64 {
        pages(namespace).reduce(0) { $0 + $1.record.bytes }
    }

    // MARK: Writing

    /// Move a staged page into place in one step. An existing copy is replaced; its pending
    /// attempts are kept only when they were made on the same version (the caller refuses to
    /// refresh a page with pending attempts, this is a second guard).
    func commit(_ namespace: String, _ staged: StagedPage, title: String) throws -> SavedPage {
        let manager = FileManager.default
        let slug = staged.bundle.slug
        let destination = pageFolder(namespace, slug)
        let existing = try? load(destination)
        if let existing, !existing.pending.isEmpty, existing.record.version != staged.bundle.version {
            throw StoreError(message: "This page has answers waiting to sync that were made on another version; sync them first.")
        }
        let bytes = staged.files.values.reduce(Int64(0)) { $0 + $1.bytes }
        let record = SavedPageRecord(
            slug: slug, title: title, savedAt: Date(), version: staged.bundle.version, serverVersion: nil,
            supersededBy: staged.bundle.supersededBy, deletedOnServer: false, bytes: bytes, files: staged.files
        )
        try staged.bundle.raw.data().write(to: staged.folder.appending(path: "bundle.json"), options: .atomic)
        if let existing, !existing.pending.isEmpty {
            try Self.encoder.encode(existing.pending).write(to: staged.folder.appending(path: "pending.json"), options: .atomic)
        }
        try Self.encoder.encode(record).write(to: staged.folder.appending(path: "page.json"), options: .atomic)
        try manager.createDirectory(at: pagesFolder(namespace), withIntermediateDirectories: true)
        if manager.fileExists(atPath: destination.path(percentEncoded: false)) {
            _ = try manager.replaceItemAt(destination, withItemAt: staged.folder)
        } else {
            try manager.moveItem(at: staged.folder, to: destination)
        }
        index[namespace] = nil
        return try load(destination)
    }

    func remove(_ namespace: String, _ slug: String) throws {
        let folder = pageFolder(namespace, slug)
        if FileManager.default.fileExists(atPath: folder.path(percentEncoded: false)) {
            try FileManager.default.removeItem(at: folder)
        }
        index[namespace] = nil
    }

    /// Add an attempt to the queue; written to disk before the page gets its reply.
    func appendPending(_ namespace: String, _ slug: String, _ attempt: PendingAttempt) throws {
        let folder = pageFolder(namespace, slug)
        var page = try load(folder)
        page.pending.append(attempt)
        try Self.encoder.encode(page.pending).write(to: folder.appending(path: "pending.json"), options: [.atomic])
    }

    /// After an upload: drop the sent attempts and, when given, put the server's bundle in
    /// place of the saved one.
    func finishUpload(_ namespace: String, _ slug: String, sent: Set<UUID>, bundle: OfflineBundle?) throws {
        let folder = pageFolder(namespace, slug)
        var page = try load(folder)
        page.pending.removeAll { sent.contains($0.clientID) }
        try Self.encoder.encode(page.pending).write(to: folder.appending(path: "pending.json"), options: [.atomic])
        if let bundle {
            try bundle.raw.data().write(to: folder.appending(path: "bundle.json"), options: .atomic)
            page.record.version = bundle.version
            page.record.serverVersion = nil
            page.record.supersededBy = bundle.supersededBy
            if bundle.title != page.record.title, !bundle.title.isEmpty { page.record.title = bundle.title }
            try Self.encoder.encode(page.record).write(to: folder.appending(path: "page.json"), options: .atomic)
        }
    }

    /// The server's bundle of a page that has no pending attempts and the same version:
    /// keep it (fresh responses). Another version: only note it (the reader decides to refresh).
    func noteServerBundle(_ namespace: String, _ bundle: OfflineBundle) throws {
        let folder = pageFolder(namespace, bundle.slug)
        guard var page = try? load(folder) else { return }
        page.record.deletedOnServer = false
        page.record.supersededBy = bundle.supersededBy
        if bundle.version == page.record.version {
            page.record.serverVersion = nil
            if page.pending.isEmpty {
                try bundle.raw.data().write(to: folder.appending(path: "bundle.json"), options: .atomic)
            }
        } else {
            page.record.serverVersion = bundle.version
        }
        try Self.encoder.encode(page.record).write(to: folder.appending(path: "page.json"), options: .atomic)
    }

    func markDeletedOnServer(_ namespace: String, _ slug: String) throws {
        let folder = pageFolder(namespace, slug)
        guard var page = try? load(folder) else { return }
        page.record.deletedOnServer = true
        try Self.encoder.encode(page.record).write(to: folder.appending(path: "page.json"), options: .atomic)
    }

    // MARK: Sync log

    func log(_ namespace: String) -> [SyncLogEntry] {
        let url = namespaceFolder(namespace).appending(path: "synclog.json")
        guard let data = try? Data(contentsOf: url) else { return [] }
        return (try? Self.decoder.decode([SyncLogEntry].self, from: data)) ?? []
    }

    func appendLog(_ namespace: String, _ entries: [SyncLogEntry]) throws {
        guard !entries.isEmpty else { return }
        try writeLog(namespace, log(namespace) + entries)
    }

    func dismissLog(_ namespace: String, ids: Set<UUID>?) throws {
        try writeLog(namespace, ids.map { ids in log(namespace).filter { !ids.contains($0.id) } } ?? [])
    }

    private func writeLog(_ namespace: String, _ entries: [SyncLogEntry]) throws {
        try FileManager.default.createDirectory(at: namespaceFolder(namespace), withIntermediateDirectories: true)
        try Self.encoder.encode(entries).write(to: namespaceFolder(namespace).appending(path: "synclog.json"), options: .atomic)
    }

    static let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }()

    static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }()
}

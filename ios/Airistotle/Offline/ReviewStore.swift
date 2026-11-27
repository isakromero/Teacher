import Foundation

/// A review answer made on the phone that waits to upload (`POST /api/offline/review/answers`).
struct PendingReviewAnswer: Codable, Sendable, Equatable, Identifiable {
    var clientID: UUID
    var itemID: Int
    var slug: String
    var title: String
    var madeAt: Instant
    /// What it was, for the sync log ("Review of …, answered …: option 2").
    var summary: String
    /// The upload item, fixed when the answer is made (the same UUID never goes with other content).
    var upload: JSONValue

    var id: UUID { clientID }
}

/// The review queue saved on the phone, in the same per-server-and-account folder as the saved
/// pages (`SavedStore`):
///
///     <root>/<namespace>/review/bundle.json     the review bundle as the server sent it
///                               answered.json   [LocalReviewAnswer] since it was made
///                               pending.json    [PendingReviewAnswer], written before a verdict shows
///                               renderer/       the web client's Markdown and maths renderer
///                                               (index.json: target -> SavedFile; files/<sha>)
///
/// The sync log stays the saved pages' (`SavedStore.appendLog`).
actor ReviewStore {
    nonisolated let root: URL

    init(root: URL) {
        self.root = root
    }

    nonisolated func folder(_ namespace: String) -> URL {
        root.appending(path: "\(namespace)/review", directoryHint: .isDirectory)
    }

    private func read<T: Decodable>(_ type: T.Type, _ url: URL) throws -> T? {
        guard FileManager.default.fileExists(atPath: url.path(percentEncoded: false)) else { return nil }
        return try SavedStore.decoder.decode(type, from: Data(contentsOf: url))
    }

    private func write(_ value: some Encodable, _ url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try SavedStore.encoder.encode(value).write(to: url, options: .atomic)
    }

    // MARK: The bundle

    /// The saved bundle; nil when none was saved. A file that cannot be read is an error.
    func bundle(_ namespace: String) throws -> ReviewBundle? {
        let url = folder(namespace).appending(path: "bundle.json")
        guard FileManager.default.fileExists(atPath: url.path(percentEncoded: false)) else { return nil }
        return try ReviewBundle(data: Data(contentsOf: url))
    }

    func answered(_ namespace: String) throws -> [LocalReviewAnswer] {
        try read([LocalReviewAnswer].self, folder(namespace).appending(path: "answered.json")) ?? []
    }

    func pending(_ namespace: String) throws -> [PendingReviewAnswer] {
        try read([PendingReviewAnswer].self, folder(namespace).appending(path: "pending.json")) ?? []
    }

    func queue(_ namespace: String) throws -> LocalReviewQueue? {
        guard let bundle = try bundle(namespace) else { return nil }
        return LocalReviewQueue(bundle: bundle, answered: try answered(namespace))
    }

    /// Put a fresh bundle in place. Answers it already counts are dropped from `answered`: those
    /// uploaded and made before it was bundled. Answers still waiting, or made after, stay.
    func saveBundle(_ namespace: String, _ bundle: ReviewBundle) throws {
        let bundledAt = bundle.bundledAt
        let kept = try answered(namespace).filter { $0.pending || $0.madeAt > bundledAt }
        try FileManager.default.createDirectory(at: folder(namespace), withIntermediateDirectories: true)
        try bundle.raw.data().write(to: folder(namespace).appending(path: "bundle.json"), options: .atomic)
        try write(kept, folder(namespace).appending(path: "answered.json"))
    }

    /// Record an answer: the queue entry (when it waits to upload) is written first.
    func record(_ namespace: String, _ answer: LocalReviewAnswer, pending upload: PendingReviewAnswer?) throws {
        if let upload {
            try write(try pending(namespace) + [upload], folder(namespace).appending(path: "pending.json"))
        }
        var all = try answered(namespace).filter { $0.itemID != answer.itemID }
        all.append(answer)
        try write(all, folder(namespace).appending(path: "answered.json"))
    }

    /// After an upload: drop the sent answers from the queue; each accepted one gets the
    /// server's schedule (`due`: client id -> (interval, due)).
    func finishUpload(_ namespace: String, sent: Set<UUID>, schedules: [UUID: (Double, Instant)]) throws {
        let queue = try pending(namespace)
        let sentItems = Dictionary(queue.filter { sent.contains($0.clientID) }.map { ($0.itemID, $0.clientID) }) { first, _ in first }
        try write(queue.filter { !sent.contains($0.clientID) }, folder(namespace).appending(path: "pending.json"))
        let updated = try answered(namespace).map { answer -> LocalReviewAnswer in
            guard let client = sentItems[answer.itemID] else { return answer }
            var copy = answer
            copy.pending = false
            if let (interval, due) = schedules[client] {
                copy.intervalDays = interval
                copy.dueAt = due
            }
            return copy
        }
        try write(updated, folder(namespace).appending(path: "answered.json"))
    }

    /// Signing out: the bundle and the record of what was answered go (they are this account's
    /// queue); answers waiting to upload stay, with the saved pages' waiting answers, for the
    /// next sign-in to this account.
    func forgetBundle(_ namespace: String) throws {
        let manager = FileManager.default
        for name in ["bundle.json", "answered.json"] {
            let url = folder(namespace).appending(path: name)
            if manager.fileExists(atPath: url.path(percentEncoded: false)) { try manager.removeItem(at: url) }
        }
    }

    // MARK: The renderer

    private func rendererFolder(_ namespace: String) -> URL { folder(namespace).appending(path: "renderer", directoryHint: .isDirectory) }

    func rendererIndex(_ namespace: String) throws -> [String: SavedFile] {
        try read([String: SavedFile].self, rendererFolder(namespace).appending(path: "index.json")) ?? [:]
    }

    /// A saved renderer file for `target`, and where it is.
    func rendererFile(_ namespace: String, target: String) throws -> (SavedFile, URL)? {
        guard let file = try rendererIndex(namespace)[target] else { return nil }
        let url = rendererFolder(namespace).appending(path: "files/\(file.file)")
        guard FileManager.default.fileExists(atPath: url.path(percentEncoded: false)) else { return nil }
        return (file, url)
    }

    /// Replace the saved renderer with `files` (target -> (status, headers, body)).
    func saveRenderer(_ namespace: String, _ files: [String: (status: Int, headers: [String: String], body: Data)]) throws {
        let staging = folder(namespace).appending(path: ".renderer-\(UUID().uuidString)", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: staging.appending(path: "files"), withIntermediateDirectories: true)
        var index: [String: SavedFile] = [:]
        for (target, answer) in files {
            let name = SavedStore.fileName(for: target)
            try answer.body.write(to: staging.appending(path: "files/\(name)"), options: .atomic)
            let kept = answer.headers.filter { $0.key.lowercased() == "content-type" }
            index[target] = SavedFile(file: name, status: answer.status, headers: kept, bytes: Int64(answer.body.count))
        }
        try write(index, staging.appending(path: "index.json"))
        let destination = rendererFolder(namespace)
        if FileManager.default.fileExists(atPath: destination.path(percentEncoded: false)) {
            _ = try FileManager.default.replaceItemAt(destination, withItemAt: staging)
        } else {
            try FileManager.default.moveItem(at: staging, to: destination)
        }
    }

    /// Bytes on disk (allocated) of everything review keeps for `namespace`.
    func diskUsage(_ namespace: String) -> Int64 {
        SavedStore.allocatedSize(of: folder(namespace))
    }
}

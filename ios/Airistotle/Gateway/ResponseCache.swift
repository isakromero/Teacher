import CryptoKit
import Foundation
import Synchronization

/// A response saved on the phone.
struct CachedResponse: Sendable, Equatable {
    struct Metadata: Codable, Sendable, Equatable {
        var target: String
        var status: Int
        var headers: [String: String]
        var storedAt: Date
        var byteCount: Int64
    }

    var metadata: Metadata
    var bodyFile: URL
}

/// The gateway's on-disk copies of successful GET responses, one folder per namespace (a
/// server and an account), in Application Support so iOS does not evict them.
///
/// Each entry is `<sha256 of the target>.body` plus `.json` metadata. A body is written to a
/// partial file while it streams and only moved into place, metadata last, once the whole
/// body arrived: a lookup never sees a half-written copy.
final class ResponseCache: Sendable {
    let root: URL
    private let fileManager = FileManagerBox()

    /// `root` defaults to `Application Support/Airistotle/ResponseCache`.
    init(root: URL) {
        self.root = root
    }

    static func defaultRoot() throws -> URL {
        let support = try FileManager.default.url(
            for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true
        )
        return support.appending(path: "Airistotle/ResponseCache", directoryHint: .isDirectory)
    }

    /// The file name stem of a target (stable across launches).
    static func key(for target: String) -> String {
        SHA256.hash(data: Data(target.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    /// A namespace name for a server and an account (`account` nil: a server without accounts).
    static func namespace(server: ServerAddress, account: String?) -> String {
        key(for: "\(server.description)|\(account ?? "-")").prefix(24).description
    }

    private func folder(_ namespace: String) -> URL {
        root.appending(path: namespace, directoryHint: .isDirectory)
    }

    // MARK: Reading

    func lookup(namespace: String, target: String) -> CachedResponse? {
        let stem = folder(namespace).appending(path: Self.key(for: target))
        let metaURL = stem.appendingPathExtension("json")
        let bodyURL = stem.appendingPathExtension("body")
        guard let data = try? Data(contentsOf: metaURL),
              let metadata = try? Self.decoder.decode(CachedResponse.Metadata.self, from: data),
              metadata.target == target,
              fileManager.fileExists(bodyURL)
        else { return nil }
        return CachedResponse(metadata: metadata, bodyFile: bodyURL)
    }

    /// The body of a cached response, read in pieces.
    static func bodyStream(of file: URL, chunkSize: Int = 256 * 1024) -> AsyncThrowingStream<Data, any Error> {
        AsyncThrowingStream { continuation in
            let task = Task.detached {
                do {
                    let handle = try FileHandle(forReadingFrom: file)
                    defer { try? handle.close() }
                    while !Task.isCancelled, let chunk = try handle.read(upToCount: chunkSize), !chunk.isEmpty {
                        continuation.yield(chunk)
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    // MARK: Writing

    /// A writer for one response; nothing is visible until `commit()`.
    func writer(namespace: String, target: String, status: Int, headers: [String: String]) throws -> CacheWriter {
        let partials = root.appending(path: ".partial", directoryHint: .isDirectory)
        try fileManager.createDirectory(partials)
        try fileManager.createDirectory(folder(namespace))
        try fileManager.excludeFromBackup(root)
        let partial = partials.appending(path: UUID().uuidString)
        let stem = folder(namespace).appending(path: Self.key(for: target))
        return try CacheWriter(
            partial: partial,
            body: stem.appendingPathExtension("body"),
            meta: stem.appendingPathExtension("json"),
            metadata: CachedResponse.Metadata(
                target: target, status: status, headers: headers, storedAt: Date(), byteCount: 0
            )
        )
    }

    // MARK: Housekeeping

    /// Bytes used by every namespace.
    func usage() -> Int64 {
        guard let enumerator = FileManager.default.enumerator(
            at: root, includingPropertiesForKeys: [.totalFileAllocatedSizeKey, .isRegularFileKey]
        ) else { return 0 }
        var total: Int64 = 0
        for case let url as URL in enumerator {
            let values = try? url.resourceValues(forKeys: [.totalFileAllocatedSizeKey, .isRegularFileKey])
            if values?.isRegularFile == true { total += Int64(values?.totalFileAllocatedSize ?? 0) }
        }
        return total
    }

    /// How many responses are saved (all namespaces).
    func entryCount() -> Int {
        guard let enumerator = FileManager.default.enumerator(at: root, includingPropertiesForKeys: nil)
        else { return 0 }
        var count = 0
        for case let url as URL in enumerator where url.pathExtension == "json" { count += 1 }
        return count
    }

    /// Delete every saved response.
    func clear() throws {
        guard fileManager.fileExists(root) else { return }
        try FileManager.default.removeItem(at: root)
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

/// Writes one response body while it streams; `commit()` makes it visible, `discard()` drops it.
final class CacheWriter: Sendable {
    private struct State {
        var handle: FileHandle?
        var metadata: CachedResponse.Metadata
        var finished = false
    }

    private let partial: URL
    private let body: URL
    private let meta: URL
    private let state: Mutex<State>

    fileprivate init(partial: URL, body: URL, meta: URL, metadata: CachedResponse.Metadata) throws {
        guard FileManager.default.createFile(atPath: partial.path(percentEncoded: false), contents: nil) else {
            throw CocoaError(.fileWriteUnknown, userInfo: [NSFilePathErrorKey: partial.path(percentEncoded: false)])
        }
        self.partial = partial
        self.body = body
        self.meta = meta
        let handle = try FileHandle(forWritingTo: partial)
        state = Mutex(State(handle: handle, metadata: metadata))
    }

    func append(_ data: Data) throws {
        try state.withLock { state in
            guard !state.finished, let handle = state.handle else { return }
            try handle.write(contentsOf: data)
            state.metadata.byteCount += Int64(data.count)
        }
    }

    func commit() throws {
        let metadata: CachedResponse.Metadata? = try state.withLock { state in
            guard !state.finished else { return nil }
            state.finished = true
            try state.handle?.close()
            state.handle = nil
            return state.metadata
        }
        guard let metadata else { return }
        let manager = FileManager.default
        try? manager.removeItem(at: meta) // an old copy's metadata goes first
        if manager.fileExists(atPath: body.path(percentEncoded: false)) {
            _ = try manager.replaceItemAt(body, withItemAt: partial)
        } else {
            try manager.moveItem(at: partial, to: body)
        }
        try ResponseCache.encoder.encode(metadata).write(to: meta, options: .atomic)
    }

    func discard() {
        state.withLock { state in
            guard !state.finished else { return }
            state.finished = true
            try? state.handle?.close()
            state.handle = nil
        }
        try? FileManager.default.removeItem(at: partial)
    }
}

/// `FileManager` calls the cache makes from any thread (`FileManager.default` is thread-safe).
private struct FileManagerBox: Sendable {
    func fileExists(_ url: URL) -> Bool {
        FileManager.default.fileExists(atPath: url.path(percentEncoded: false))
    }

    func createDirectory(_ url: URL) throws {
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    }

    func excludeFromBackup(_ url: URL) throws {
        var url = url
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try url.setResourceValues(values)
    }
}

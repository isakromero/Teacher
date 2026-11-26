import Foundation
import Synchronization

/// Where written answers are graded.
enum WrittenAnswerGrading: String, CaseIterable, Sendable, Identifiable {
    /// On the phone only when the server cannot be reached (the default).
    case offlineOnly
    /// Always on the phone; online, the grade is uploaded straight away.
    case always

    var id: Self { self }

    var label: String {
        switch self {
        case .offlineOnly: "On this phone only when offline"
        case .always: "Always on this phone"
        }
    }
}

/// The reader's offline settings (UserDefaults is safe to use from any thread).
struct OfflinePreferences: @unchecked Sendable {
    let defaults: UserDefaults
    static let gradingKey = "writtenAnswerGrading"

    var grading: WrittenAnswerGrading {
        get { WrittenAnswerGrading(rawValue: defaults.string(forKey: Self.gradingKey) ?? "") ?? .offlineOnly }
        nonmutating set { defaults.set(newValue.rawValue, forKey: Self.gradingKey) }
    }
}

/// The phone's side of offline reading (`teacher.web.routers.offline`): saving pages, answering
/// a saved page's reading endpoints when the server cannot be reached, grading on the phone,
/// and uploading what was answered here.
///
/// It plugs into the gateway three ways:
/// - before the network (`GatewayRoute`): a written answer when grading is "Always on this
///   phone"; and a saved page's reading endpoints while it has answers waiting to sync (they
///   are synced first, so the server, the authority, sees them before anything else);
/// - when the server cannot be reached (`OfflineRoute`): a saved page's reading endpoints are
///   answered by `ReadingEngine`, and its other GETs from the saved files;
/// - after the server answered (`GatewayObserver`): a saved page's bundle is refreshed when the
///   page is opened and after an answer.
final class OfflineService: GatewayRoute, OfflineRoute, GatewayObserver, Sendable {
    let name = ReadingEngine.sourceName
    static let choiceGrader = "device:airistotle-rules"

    let store: SavedStore
    let gateway: Gateway
    let grader: BlurbGrader
    let preferences: OfflinePreferences
    private let changed: @Sendable () -> Void
    private let submitLock = AsyncLock()
    private let syncLock = AsyncLock()
    private let clock: @Sendable () -> Instant

    init(
        gateway: Gateway,
        store: SavedStore,
        judge: any BlurbJudge,
        preferences: OfflinePreferences,
        clock: @escaping @Sendable () -> Instant = { Instant.now() },
        changed: @escaping @Sendable () -> Void
    ) {
        self.gateway = gateway
        self.store = store
        self.grader = BlurbGrader(judge: judge)
        self.preferences = preferences
        self.clock = clock
        self.changed = changed
    }

    /// Put the service in front of the gateway.
    func install() {
        gateway.install(route: self, offlineRoute: self, observer: self)
    }

    var namespace: String? { gateway.connection?.cacheNamespace }

    // MARK: Which requests are a saved page's

    enum ReadingRoute: Equatable {
        case blocks(slug: String)
        case responses(slug: String, key: String)
        case submit(slug: String, key: String)
    }

    static func readingRoute(_ request: GatewayRequest) -> ReadingRoute? {
        let parts = request.path.split(separator: "/", omittingEmptySubsequences: false).map {
            String($0).removingPercentEncoding ?? String($0)
        }
        // "", "api", "artifacts", slug, "blocks", [key, "responses"]
        guard parts.count >= 5, parts[0].isEmpty, parts[1] == "api", parts[2] == "artifacts", parts[4] == "blocks" else { return nil }
        let slug = parts[3]
        if parts.count == 5, request.method == "GET" { return .blocks(slug: slug) }
        if parts.count == 7, parts[6] == "responses" {
            if request.method == "GET" { return .responses(slug: slug, key: parts[5]) }
            if request.method == "POST" { return .submit(slug: slug, key: parts[5]) }
        }
        return nil
    }

    /// The slug of a study page target (`/artifacts/{slug}`), else nil.
    static func pageSlug(_ path: String) -> String? {
        let parts = path.split(separator: "/", omittingEmptySubsequences: false)
        guard parts.count == 3, parts[0].isEmpty, parts[1] == "artifacts", !parts[2].isEmpty else { return nil }
        return String(parts[2]).removingPercentEncoding
    }

    // MARK: GatewayRoute: before the network

    func reply(to request: GatewayRequest, context: GatewayContext) async -> GatewayReply? {
        guard let route = Self.readingRoute(request) else { return nil }
        let namespace = context.connection.cacheNamespace
        if case .submit(let slug, let key) = route, preferences.grading == .always,
           let reply = await gradeOnPhoneAndUpload(slug: slug, key: key, request: request, namespace: namespace) {
            return reply
        }
        // Answers waiting to sync go first; if they cannot, the phone answers (the server
        // would not show them).
        guard let page = await store.page(namespace, route.slug), !page.pending.isEmpty else { return nil }
        _ = await sync(slugs: [route.slug])
        guard let after = await store.page(namespace, route.slug), !after.pending.isEmpty else { return nil }
        return await answerLocally(route, request: request, namespace: namespace)
    }

    // MARK: OfflineRoute: the server cannot be reached

    func reply(to request: GatewayRequest, context: GatewayContext, failure: URLError) async -> GatewayReply? {
        let namespace = context.connection.cacheNamespace
        if let route = Self.readingRoute(request) {
            guard await store.isSaved(namespace, route.slug) else { return nil }
            return await answerLocally(route, request: request, namespace: namespace)
        }
        guard request.isReadOnly, let (file, url) = await store.file(namespace, target: request.target) else { return nil }
        var headers = file.headers
        headers[Gateway.sourceHeader] = "saved-page"
        headers["Content-Length"] = String(file.bytes)
        let body: AsyncThrowingStream<Data, any Error> = request.method == "HEAD" ? .single(Data()) : ResponseCache.bodyStream(of: url)
        return GatewayReply(status: file.status, headers: headers, body: body, source: .local(name))
    }

    // MARK: GatewayObserver: keep saved bundles fresh

    func serverReplied(to request: GatewayRequest, status: Int, headers: [String: String], context: GatewayContext) async {
        let namespace = context.connection.cacheNamespace
        var slug: String?
        if request.isMainFrame, request.method == "GET", status == 200, let page = Self.pageSlug(request.path) {
            slug = page
        } else if case .submit(let page, _) = Self.readingRoute(request), status == 201 {
            slug = page
        }
        guard let slug, await store.isSaved(namespace, slug) else { return }
        // After the reply is on its way: refreshing must not hold up the page.
        Task.detached { [self] in await refreshBundle(slug: slug, namespace: namespace) }
    }

    /// Fetch the server's bundle and keep it (same version, nothing waiting) or note the change.
    func refreshBundle(slug: String, namespace: String) async {
        guard let answer = try? await gateway.fetch("GET", "/api/offline/artifacts/\(StudyPageRequests.segment(slug))") else { return }
        if answer.status == 404 {
            try? await store.markDeletedOnServer(namespace, slug)
        } else if answer.status == 200, let bundle = try? OfflineBundle(data: answer.body), bundle.slug == slug {
            try? await store.noteServerBundle(namespace, bundle)
        }
        changed()
    }

    // MARK: Answering on the phone

    func answerLocally(_ route: ReadingRoute, request: GatewayRequest, namespace: String) async -> GatewayReply {
        switch route {
        case .blocks(let slug):
            guard let page = await store.page(namespace, slug) else { return Self.notSaved(slug) }
            return Self.json(200, page.engine.blocksView())
        case .responses(let slug, let key):
            guard let page = await store.page(namespace, slug) else { return Self.notSaved(slug) }
            do {
                return Self.json(200, try page.engine.responsesView(key: key))
            } catch {
                return error.reply
            }
        case .submit(let slug, let key):
            return await submitLocally(slug: slug, key: key, body: request.body, namespace: namespace)
        }
    }

    /// Grade and record an attempt on the phone; it is queued (on disk) before the reply.
    func submitLocally(slug: String, key: String, body: Data?, namespace: String) async -> GatewayReply {
        await submitLock.run {
            guard let page = await store.page(namespace, slug) else { return Self.notSaved(slug) }
            let engine = page.engine
            let submission: ReadingEngine.Submission
            do {
                submission = try engine.prepare(key: key, body: Self.parseBody(body), onlyOpenGates: true)
            } catch let error as EngineError {
                return error.reply
            } catch {
                return .jsonError(500, error.localizedDescription, source: .local(name))
            }
            let grade: Grade
            do {
                grade = try await self.grade(submission, engine: engine)
            } catch {
                return .jsonError(503, error.localizedDescription, source: .local(name))
            }
            let madeAt = clock()
            let recorded = engine.record(submission, grade: grade, madeAt: madeAt)
            let attempt = Self.pendingAttempt(submission, grade: grade, row: recorded.row, madeAt: madeAt, version: page.bundle.version)
            do {
                try await store.appendPending(namespace, slug, attempt)
            } catch {
                return .jsonError(500, "The answer could not be saved on this phone (\(error.localizedDescription)), "
                    + "so it was not recorded. Try again.", source: .local(name))
            }
            changed()
            return Self.json(201, recorded.reply)
        }
    }

    func grade(_ submission: ReadingEngine.Submission, engine: ReadingEngine) async throws(JudgeFailure) -> Grade {
        if let grade = engine.deterministicGrade(submission) { return grade }
        return try await grader.grade(block: submission.block, answer: submission.answerText ?? "")
    }

    /// "Always on this phone" for a written answer while the server can be reached: fetch the
    /// page's bundle, grade on the phone, upload the attempt at once and answer from the
    /// server's result. Nil when this is no written answer (the server grades it) or the
    /// server cannot be reached (then the offline route answers, for a saved page).
    func gradeOnPhoneAndUpload(slug: String, key: String, request: GatewayRequest, namespace: String) async -> GatewayReply? {
        await submitLock.run { () async -> GatewayReply? in
            if let saved = await store.page(namespace, slug), !saved.pending.isEmpty {
                _ = await sync(slugs: [slug])
            }
            let target = "/api/offline/artifacts/\(StudyPageRequests.segment(slug))"
            let answer: (status: Int, headers: [String: String], body: Data)
            do {
                answer = try await gateway.fetch("GET", target)
            } catch {
                return nil
            }
            guard answer.status == 200, let bundle = try? OfflineBundle(data: answer.body) else { return nil }
            let engine = ReadingEngine(bundle: bundle)
            guard let block = engine.bundle.blocks.first(where: { $0["key"].string == key }), block["type"].string == "blurb" else {
                return nil
            }
            let submission: ReadingEngine.Submission
            do {
                submission = try engine.prepare(key: key, body: Self.parseBody(request.body), onlyOpenGates: true)
            } catch let error as EngineError {
                return error.reply
            } catch {
                return .jsonError(500, error.localizedDescription, source: .local(name))
            }
            let grade: Grade
            do {
                grade = try await grader.grade(block: submission.block, answer: submission.answerText ?? "")
            } catch {
                return .jsonError(503, error.localizedDescription, source: .local(name))
            }
            let madeAt = clock()
            let recorded = engine.record(submission, grade: grade, madeAt: madeAt)
            let attempt = Self.pendingAttempt(submission, grade: grade, row: recorded.row, madeAt: madeAt, version: bundle.version)
            let upload: (status: Int, headers: [String: String], body: Data)
            do {
                upload = try await gateway.fetch("POST", target + "/attempts",
                                                 json: .object(["version": .string(bundle.version), "attempts": .array([attempt.upload])]))
            } catch {
                return .jsonError(503, "The answer was graded on this phone but could not be uploaded (\(error.localizedDescription)). "
                    + "Nothing was recorded; try again.", source: .local(name))
            }
            guard upload.status == 200, let body = try? JSONValue(data: upload.body),
                  let result = body["results"].array?.first, let fresh = try? OfflineBundle(body["bundle"]) else {
                let detail = AuthClient.detail(of: upload.body) ?? "HTTP \(upload.status)"
                return .jsonError(upload.status == 200 ? 502 : upload.status,
                                  "The server did not take the answer graded on this phone: \(detail). Nothing was recorded.",
                                  source: .local(name))
            }
            if await store.isSaved(namespace, slug) {
                try? await store.finishUpload(namespace, slug, sent: [], bundle: fresh)
                changed()
            }
            guard result["status"].string != "rejected" else {
                return .jsonError(409, "The server did not accept the answer graded on this phone "
                    + "(\(result["reason"].string ?? "rejected")): \(result["detail"].string ?? ""). Nothing was recorded.",
                                  source: .local(name))
            }
            let reply = ReadingEngine.reply(before: engine, after: ReadingEngine(bundle: fresh), key: key, recorded: result["recorded"])
            return Self.json(201, reply)
        }
    }

    static func pendingAttempt(
        _ submission: ReadingEngine.Submission, grade: Grade, row: JSONValue, madeAt: Instant, version: String
    ) -> PendingAttempt {
        let clientID = UUID()
        let deviceGrader = grade.grader.hasPrefix("device:") ? grade.grader : choiceGrader
        let upload = JSONValue.object([
            "client_id": .string(clientID.uuidString.lowercased()),
            "key": .string(submission.key),
            "response": submission.response,
            "confidence": submission.confidence.map(JSONValue.string) ?? .null,
            "verdict": .string(grade.verdict),
            "feedback": grade.feedback.map(JSONValue.string) ?? .null,
            "missing": .array(grade.missing.map(JSONValue.string)),
            "details": grade.details.object == nil ? .null : grade.details,
            "grader": .string(deviceGrader),
            "made_at": .string(madeAt.iso)
        ])
        return PendingAttempt(
            clientID: clientID, key: submission.key, blockType: submission.type, bundleVersion: version,
            madeAt: madeAt, row: row, upload: upload
        )
    }

    static func parseBody(_ body: Data?) -> JSONValue {
        guard let body, let value = try? JSONValue(data: body) else { return .null }
        return value
    }

    static func json(_ status: Int, _ value: JSONValue) -> GatewayReply {
        GatewayReply(status: status, headers: ["Content-Type": "application/json", Gateway.sourceHeader: "saved-page"],
                     data: value.data(), source: .local(ReadingEngine.sourceName))
    }

    static func notSaved(_ slug: String) -> GatewayReply {
        .jsonError(503, "The page \(slug) is not saved on this phone, so it cannot be read offline.",
                   source: .local(ReadingEngine.sourceName))
    }

    // MARK: Saving

    func save(slug: String, progress: @escaping @Sendable (Int, Int) -> Void = { _, _ in }) async throws -> SavedPage {
        guard let namespace else { throw PageSaver.Failure(message: "Sign in first.") }
        if let existing = await store.page(namespace, slug), !existing.pending.isEmpty {
            _ = await sync(slugs: [slug])
            if let still = await store.page(namespace, slug), !still.pending.isEmpty {
                throw PageSaver.Failure(message: "This page has \(still.pending.count) answer(s) waiting to sync. "
                    + "Refreshing would mix them with a new copy; sync first (the server must be reachable).")
            }
        }
        let saver = PageSaver(gateway: gateway, store: store)
        let staged = try await saver.stage(slug: slug, progress: progress)
        do {
            let page = try await store.commit(namespace, staged, title: staged.bundle.title)
            changed()
            return page
        } catch {
            store.discardStaging(staged.folder)
            throw PageSaver.Failure(message: "The page could not be stored on this phone: \(error.localizedDescription) Nothing was saved.")
        }
    }

    func remove(slug: String) async throws {
        guard let namespace else { return }
        try await store.remove(namespace, slug)
        changed()
    }

    // MARK: Sync

    struct SyncSummary: Sendable, Equatable {
        var uploaded = 0
        var logged = 0
        var waiting = 0
        var unreachable = false
    }

    /// Upload every waiting attempt (of `slugs`, or of every saved page), oldest first, one
    /// upload per page and bundle version; log every rejection, mismatch and failure.
    @discardableResult
    func sync(slugs: [String]? = nil) async -> SyncSummary {
        await syncLock.run {
            guard let namespace else { return SyncSummary() }
            var summary = SyncSummary()
            let pages = await store.pages(namespace).filter { !$0.pending.isEmpty && (slugs?.contains($0.record.slug) ?? true) }
            for page in pages {
                if summary.unreachable { break }
                await syncPage(page, namespace: namespace, summary: &summary)
            }
            summary.waiting = await store.pages(namespace).reduce(0) { $0 + $1.pending.count }
            changed()
            return summary
        }
    }

    private func syncPage(_ page: SavedPage, namespace: String, summary: inout SyncSummary) async {
        let slug = page.record.slug
        var versions: [String] = []
        for attempt in page.pending where !versions.contains(attempt.bundleVersion) { versions.append(attempt.bundleVersion) }
        for version in versions {
            guard let current = await store.page(namespace, slug) else { return }
            let batch = Array(current.pending.filter { $0.bundleVersion == version }.prefix(500))
            if batch.isEmpty { continue }
            let target = "/api/offline/artifacts/\(StudyPageRequests.segment(slug))/attempts"
            let answer: (status: Int, headers: [String: String], body: Data)
            do {
                answer = try await gateway.fetch("POST", target, json: .object([
                    "version": .string(version), "attempts": .array(batch.map(\.upload))
                ]))
            } catch .unreachable {
                summary.unreachable = true
                return
            } catch {
                summary.logged += await log(namespace, page, .failed, "Uploading failed: \(error.localizedDescription) "
                    + "The answers stay on this phone and are tried again.")
                return
            }
            let sent = Set(batch.map(\.clientID))
            switch answer.status {
            case 200:
                guard let body = try? JSONValue(data: answer.body), let results = body["results"].array,
                      let fresh = try? OfflineBundle(body["bundle"]) else {
                    summary.logged += await log(namespace, page, .failed, "The server's answer to an upload could not be read. "
                        + "The answers stay on this phone and are tried again.")
                    return
                }
                var entries: [SyncLogEntry] = []
                for (attempt, result) in zip(batch, results) {
                    entries += Self.logEntries(for: result, attempt: attempt, page: page)
                    if result["status"].string != "rejected" { summary.uploaded += 1 }
                }
                if body["version_matched"].bool == false {
                    entries.append(Self.entry(page, .regenerated, "The page was regenerated on the server. The saved copy now "
                        + "holds the new page's answers; refresh it to get the new page's text."))
                }
                try? await store.finishUpload(namespace, slug, sent: sent, bundle: fresh)
                try? await store.appendLog(namespace, entries)
                summary.logged += entries.count
            case 404:
                let entries = batch.map { Self.entry(page, .deleted, "The page was deleted on the server, so this answer can never be "
                    + "uploaded. It was removed from the queue.", attempt: $0) }
                try? await store.finishUpload(namespace, slug, sent: sent, bundle: nil)
                try? await store.markDeletedOnServer(namespace, slug)
                try? await store.appendLog(namespace, entries)
                summary.logged += entries.count
                return
            case 401:
                return // the app asks to sign in again; the answers wait
            default:
                let detail = AuthClient.detail(of: answer.body) ?? "HTTP \(answer.status)"
                summary.logged += await log(namespace, page, .failed, "The server refused the upload (\(answer.status): \(detail)). "
                    + "The answers stay on this phone and are tried again.")
                return
            }
        }
    }

    private func log(_ namespace: String, _ page: SavedPage, _ kind: SyncLogEntry.Kind, _ message: String) async -> Int {
        try? await store.appendLog(namespace, [Self.entry(page, kind, message)])
        return 1
    }

    static func entry(_ page: SavedPage, _ kind: SyncLogEntry.Kind, _ message: String, attempt: PendingAttempt? = nil) -> SyncLogEntry {
        SyncLogEntry(id: UUID(), date: Date(), slug: page.record.slug, title: page.record.title, kind: kind, message: message,
                     attempt: attempt.map(describe))
    }

    static func logEntries(for result: JSONValue, attempt: PendingAttempt, page: SavedPage) -> [SyncLogEntry] {
        if result["status"].string == "rejected" {
            let reason = result["reason"].string ?? "rejected"
            let detail = result["detail"].string ?? ""
            return [entry(page, .rejected, "The server did not accept this answer (\(reason)): \(detail)", attempt: attempt)]
        }
        if result["verdict_mismatch"].bool == true {
            let phone = result["device_verdict"].string ?? "?"
            let server = result["recorded"]["verdict"].string ?? "?"
            return [entry(page, .mismatch, "The server graded this answer \(server); the phone had graded it \(phone). "
                + "The server's grade is the one kept.", attempt: attempt)]
        }
        return []
    }

    static func describe(_ attempt: PendingAttempt) -> String {
        let response = attempt.upload["response"]
        let answer: String
        if let text = response["text"].string {
            answer = "“\(text.count > 120 ? String(text.prefix(120)) + "…" : text)”"
        } else if let choice = response["choice"].int {
            answer = "option \(choice + 1)"
        } else {
            answer = String(bytes: response.data(), encoding: .utf8) ?? "(unreadable)"
        }
        return "Checkpoint \(attempt.key), answered \(attempt.madeAt.date.formatted(date: .abbreviated, time: .shortened)): \(answer)"
    }
}

extension OfflineService.ReadingRoute {
    var slug: String {
        switch self {
        case .blocks(let slug), .responses(let slug, _), .submit(let slug, _): slug
        }
    }
}

/// Runs async sections one at a time, in arrival order.
final class AsyncLock: Sendable {
    private let state = Mutex<(busy: Bool, waiters: [CheckedContinuation<Void, Never>])>((false, []))

    func run<T: Sendable>(_ body: () async -> T) async -> T {
        await acquire()
        let result = await body()
        release()
        return result
    }

    private func acquire() async {
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            let resumeNow = state.withLock { state -> Bool in
                if state.busy {
                    state.waiters.append(continuation)
                    return false
                }
                state.busy = true
                return true
            }
            if resumeNow { continuation.resume() }
        }
    }

    private func release() {
        let next = state.withLock { state -> CheckedContinuation<Void, Never>? in
            if state.waiters.isEmpty {
                state.busy = false
                return nil
            }
            return state.waiters.removeFirst()
        }
        next?.resume()
    }
}

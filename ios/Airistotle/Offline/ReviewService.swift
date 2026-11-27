import Foundation

/// Why a review request gave no result. `unreachable` is the only one that means "use the
/// questions saved on the phone"; every other one is shown as it is.
enum ReviewFailure: Error, Equatable, LocalizedError {
    case notConnected
    /// The server could not be reached (the connection failed, not the server).
    case unreachable(String)
    /// The server answered with an error (its status and `detail`).
    case refused(status: Int, detail: String)
    /// The server's answer could not be read.
    case unreadable(String)
    /// The answer is malformed (the server's own 422 rule, checked on the phone): nothing recorded.
    case invalid(String)
    /// The phone cannot grade this answer (the on-device model): nothing recorded.
    case grading(String)
    /// Offline, and the phone has nothing saved to answer from.
    case notSaved(String)
    /// The answer could not be stored on the phone: nothing recorded.
    case storage(String)

    var errorDescription: String? {
        switch self {
        case .notConnected: "The app is not connected to a server. Sign in again."
        case .unreachable(let reason): "The server could not be reached (\(reason))."
        case .refused(let status, let detail): "The server refused this (\(status)): \(detail)"
        case .unreadable(let what): "The server's answer could not be read: \(what)"
        case .invalid(let detail), .grading(let detail), .notSaved(let detail): detail
        case .storage(let detail): "The answer could not be saved on this phone (\(detail)), so it was not recorded. Try again."
        }
    }
}

/// One page of `GET /api/review/queue`.
struct ReviewQueuePage: Sendable, Equatable {
    var items: [ReviewItem]
    var due: Int
    var cap: Int
    var doneToday: Int
    var remainingToday: Int
    var itemsTotal: Int
    var nextDueAt: Instant?

    init(_ value: JSONValue) throws(ReviewFailure) {
        guard let due = value["due"].int, let cap = value["cap"].int, let done = value["done_today"].int,
              let remaining = value["remaining_today"].int, let total = value["items_total"].int,
              let items = value["items"].array else { throw .unreadable("the review queue is missing fields") }
        self.items = try items.map { item throws(ReviewFailure) in
            guard let parsed = ReviewItem(item) else { throw .unreadable("a question of the review queue (\(item["id"]))") }
            return parsed
        }
        (self.due, self.cap, doneToday, remainingToday, itemsTotal) = (due, cap, done, remaining, total)
        nextDueAt = value["next_due_at"].string.flatMap { Instant(isoString: $0) }
    }
}

/// A graded review, as the screen shows it.
struct ReviewResult: Sendable, Equatable {
    var verdict: String
    var feedback: String?
    var missing: [String]
    /// The answer key in the order shown (the correct option and explanations, or the model answer).
    var revealed: JSONValue
    var intervalDays: Double
    var dueAt: Instant
    /// Graded on this phone (offline); it uploads later.
    var gradedOnPhone: Bool

    var outcome: AnswerOutcome { AnswerOutcome(rawValue: verdict) ?? .failed }
}

/// The review queue: online through the server's endpoints (`routers/review.py`), offline from
/// the saved bundle (`routers/offline_review.py`), graded on the phone and uploaded later with
/// the saved pages' sync (`OfflineCenter.syncNow`), rejections in the same sync log.
final class ReviewService: Sendable {
    static let bundleDays = 7
    static let defaultCap = 20
    /// The web client's Markdown and maths renderer (`static/js/markdown.js`) and its styles.
    static let rendererModule = "/static/js/markdown.js"
    static let rendererStyles = "/static/css/app.css"

    let gateway: Gateway
    let store: ReviewStore
    let pages: SavedStore
    let grader: BlurbGrader
    private let clock: @Sendable () -> Instant
    private let timeZone: @Sendable () -> TimeZone
    private let changed: @Sendable () -> Void
    private let lock = AsyncLock()
    private let syncLock = AsyncLock()

    init(gateway: Gateway, store: ReviewStore, pages: SavedStore, grader: BlurbGrader,
         clock: @escaping @Sendable () -> Instant = { Instant.now() },
         timeZone: @escaping @Sendable () -> TimeZone = { TimeZone.current },
         changed: @escaping @Sendable () -> Void) {
        self.gateway = gateway
        self.store = store
        self.pages = pages
        self.grader = grader
        self.clock = clock
        self.timeZone = timeZone
        self.changed = changed
    }

    var namespace: String? { gateway.connection?.cacheNamespace }
    func now() -> Instant { clock() }

    private func fetch(_ method: String, _ target: String, json: JSONValue? = nil) async throws(ReviewFailure)
        -> (status: Int, body: Data) {
        do {
            let answer = try await gateway.fetch(method, target, json: json)
            return (answer.status, answer.body)
        } catch .notConnected {
            throw .notConnected
        } catch .unreachable(let error) {
            throw .unreachable(error.localizedDescription)
        } catch {
            // The request failed before the server answered (not a refusal by the server).
            throw .unreachable(error.localizedDescription)
        }
    }

    private static func refused(_ status: Int, _ body: Data) -> ReviewFailure {
        .refused(status: status, detail: AuthClient.detail(of: body) ?? "HTTP \(status)")
    }

    // MARK: Online

    /// `GET /api/review/queue`: the next `limit` questions due now.
    func queue(cap: Int = defaultCap, limit: Int = 1) async throws(ReviewFailure) -> ReviewQueuePage {
        let query = StudyPageRequests.formEncode([("tz", timeZone().identifier), ("cap", String(cap)), ("limit", String(limit))])
        let answer = try await fetch("GET", "/api/review/queue?\(query)")
        guard answer.status == 200 else { throw Self.refused(answer.status, answer.body) }
        guard let value = try? JSONValue(data: answer.body) else { throw .unreadable("not JSON") }
        return try ReviewQueuePage(value)
    }

    /// `POST /api/review/items/{id}/answers`: graded by the server. The question also leaves the
    /// saved queue (its token is used up).
    func answerOnline(_ item: ReviewItem, response: JSONValue, confidence: ReviewConfidence) async throws(ReviewFailure)
        -> ReviewResult {
        let body = JSONValue.object([
            "presentation": .string(item.presentation), "response": response, "confidence": .string(confidence.rawValue)
        ])
        let answer = try await fetch("POST", "/api/review/items/\(item.id)/answers", json: body)
        guard answer.status == 201 else { throw Self.refused(answer.status, answer.body) }
        guard let value = try? JSONValue(data: answer.body), let verdict = value["verdict"].string,
              let interval = value["interval_days"].number, let due = Instant(isoString: value["due_at"].string ?? "") else {
            throw .unreadable("the graded review is missing fields")
        }
        let result = ReviewResult(
            verdict: verdict, feedback: value["feedback"].string, missing: (value["missing"].array ?? []).compactMap(\.string),
            revealed: value["revealed"], intervalDays: interval, dueAt: due, gradedOnPhone: false
        )
        if let namespace {
            let record = LocalReviewAnswer(itemID: item.id, presentation: item.presentation, slug: item.artifactSlug, madeAt: clock(),
                                           verdict: verdict, intervalDays: interval, dueAt: due, pending: false)
            // The server has the answer; failing to note it here only means the saved queue may
            // offer the question again, whose upload the server then rejects (and the log says so).
            do { try await store.record(namespace, record, pending: nil) } catch {
                try? await pages.appendLog(namespace, [SyncLogEntry(
                    id: UUID(), date: Date(), slug: item.artifactSlug, title: "Review: \(item.artifactTitle)", kind: .failed,
                    message: "The phone could not note that this review was answered (\(error.localizedDescription)); "
                        + "it may be offered again offline.", attempt: nil)])
            }
            changed()
        }
        return result
    }

    // MARK: The saved bundle

    /// The saved queue (nil: none saved).
    func localQueue() async throws -> LocalReviewQueue? {
        guard let namespace else { return nil }
        return try await store.queue(namespace)
    }

    func pendingAnswers() async -> [PendingReviewAnswer] {
        guard let namespace else { return [] }
        return (try? await store.pending(namespace)) ?? []
    }

    /// Fetch and save the review bundle and, the first time, the renderer its questions need.
    /// Throws when the bundle could not be saved; a renderer that could not be saved is
    /// returned (the questions are saved, but their maths cannot be drawn offline).
    @discardableResult
    func refreshBundle() async throws(ReviewFailure) -> ReviewFailure? {
        guard let namespace else { throw .notConnected }
        let query = StudyPageRequests.formEncode([
            ("tz", timeZone().identifier), ("days", String(Self.bundleDays)), ("cap", String(Self.defaultCap))
        ])
        let answer = try await fetch("GET", "/api/offline/review?\(query)")
        guard answer.status == 200 else { throw Self.refused(answer.status, answer.body) }
        let bundle: ReviewBundle
        do {
            bundle = try ReviewBundle(data: answer.body)
        } catch {
            throw .unreadable(error.message)
        }
        do {
            try await store.saveBundle(namespace, bundle)
        } catch {
            throw .storage(error.localizedDescription)
        }
        changed()
        guard (try? await store.rendererIndex(namespace))?[Self.rendererModule] == nil else { return nil }
        do {
            try await saveRenderer(namespace: namespace)
            return nil
        } catch {
            return error
        }
    }

    /// Save the web client's renderer: `markdown.js` and every module it imports, the app's
    /// stylesheet and KaTeX's (found in the review page), with their fonts.
    func saveRenderer(namespace: String) async throws(ReviewFailure) {
        let page = try await fetch("GET", "/review")
        guard page.status == 200 else { throw Self.refused(page.status, page.body) }
        let katex = StaticCrawler.pageReferences(String(bytes: page.body, encoding: .utf8) ?? "")
            .filter { $0.contains("katex") && $0.hasSuffix(".css") }
        guard !katex.isEmpty else { throw .unreadable("the review page links no KaTeX stylesheet, so maths cannot be saved") }
        var queue = [Self.rendererModule, Self.rendererStyles] + katex
        var seen = Set(queue)
        var files: [String: (status: Int, headers: [String: String], body: Data)] = [:]
        while let target = queue.first {
            queue.removeFirst()
            let answer: (status: Int, headers: [String: String], body: Data)
            do {
                answer = try await gateway.fetch("GET", target, accept: "*/*")
            } catch .unreachable(let error) {
                throw .unreachable("\(target): \(error.localizedDescription)")
            } catch {
                throw .unreadable("\(target): \(error.localizedDescription)")
            }
            guard answer.status == 200 else { throw Self.refused(answer.status, answer.body) }
            files[target] = answer
            for next in StaticCrawler.references(of: target, content: String(bytes: answer.body, encoding: .utf8) ?? "")
            where !seen.contains(next) {
                seen.insert(next)
                queue.append(next)
            }
        }
        do {
            try await store.saveRenderer(namespace, files)
        } catch {
            throw .storage(error.localizedDescription)
        }
    }

    // MARK: Offline

    /// Grade an answer on the phone and record it (queued on disk before the result shows).
    func answerOffline(_ item: ReviewItem, response: JSONValue, confidence: ReviewConfidence?) async throws(ReviewFailure)
        -> ReviewResult {
        guard let namespace else { throw .notConnected }
        return try await lock.run { () async -> Result<ReviewResult, ReviewFailure> in
            do throws(ReviewFailure) {
                return .success(try await gradeAndRecord(item, response: response, confidence: confidence, namespace: namespace))
            } catch {
                return .failure(error)
            }
        }.get()
    }

    private func gradeAndRecord(_ item: ReviewItem, response: JSONValue, confidence: ReviewConfidence?, namespace: String)
        async throws(ReviewFailure) -> ReviewResult {
        let queue: LocalReviewQueue?
        do {
            queue = try await store.queue(namespace)
        } catch {
            throw .notSaved("The review questions saved on this phone cannot be read: \(error.localizedDescription)")
        }
        guard let queue, let saved = queue.bundle.items.first(where: { $0.id == item.id }),
              saved.presentation == item.presentation else {
            throw .notSaved("This question is not saved on this phone in the form shown, so it cannot be answered offline. "
                + "Answer it when the server can be reached.")
        }
        guard !queue.answered.contains(where: { $0.itemID == item.id }) else {
            throw .invalid("This question was already answered on this phone; it comes back when it is due again.")
        }
        let answer: ReviewGrading.Answer
        do {
            answer = try ReviewGrading.check(saved, response: response, confidence: confidence,
                                             defaultMinWords: queue.bundle.defaultMinWords)
        } catch {
            throw .invalid(error.detail)
        }
        let grade: Grade
        if let byKey = ReviewGrading.choiceGrade(answer, grading: queue.bundle.grading) {
            grade = byKey
        } else {
            do {
                grade = try await grader.grade(block: ReviewGrading.judgeBlock(saved), answer: answer.text ?? "")
            } catch {
                throw .grading(error.localizedDescription)
            }
        }
        let madeAt = clock()
        let interval = queue.bundle.schedule.interval(after: grade.verdict, from: saved.intervalDays)
        let due = madeAt.adding(days: interval)
        let clientID = UUID()
        let pending = PendingReviewAnswer(
            clientID: clientID, itemID: saved.id, slug: saved.artifactSlug, title: saved.artifactTitle, madeAt: madeAt,
            summary: Self.describe(saved, response: response, madeAt: madeAt),
            upload: ReviewGrading.upload(answer, grade: grade, clientID: clientID, madeAt: madeAt)
        )
        let record = LocalReviewAnswer(itemID: saved.id, presentation: saved.presentation, slug: saved.artifactSlug, madeAt: madeAt,
                                       verdict: grade.verdict, intervalDays: interval, dueAt: due, pending: true)
        do {
            try await store.record(namespace, record, pending: pending)
        } catch {
            throw .storage(error.localizedDescription)
        }
        changed()
        return ReviewResult(verdict: grade.verdict, feedback: grade.feedback, missing: grade.missing, revealed: saved.answerKey,
                            intervalDays: interval, dueAt: due, gradedOnPhone: true)
    }

    static func describe(_ item: ReviewItem, response: JSONValue, madeAt: Instant) -> String {
        let answer: String
        if let text = response["text"].string {
            answer = "“\(text.count > 120 ? String(text.prefix(120)) + "…" : text)”"
        } else if let choice = response["choice"].int {
            answer = "option \(choice + 1)" + (choice < item.options.count ? " (\(SpeakableText.from(item.options[choice])))" : "")
        } else {
            answer = String(bytes: response.data(), encoding: .utf8) ?? "(unreadable)"
        }
        // Maths as words: the log is plain text, and raw TeX is never shown.
        let words = SpeakableText.from(item.prompt)
        let prompt = words.count > 80 ? String(words.prefix(80)) + "…" : words
        return "Review “\(prompt)”, answered \(madeAt.date.formatted(date: .abbreviated, time: .shortened)): \(answer)"
    }
}

// MARK: - Uploading answers made offline

extension ReviewService {
    struct SyncSummary: Sendable, Equatable {
        var uploaded = 0
        var logged = 0
        var unreachable = false
    }

    /// Upload every waiting answer (oldest first, at most 500 per request); log every
    /// rejection, every regrade that differs and every failure in the saved pages' sync log.
    /// Accepted answers take the server's schedule; then the bundle is fetched again.
    @discardableResult
    func sync() async -> SyncSummary {
        guard let namespace else { return SyncSummary() }
        let summary = await lockedSync(namespace)
        changed()
        if summary.uploaded > 0 {
            do { try await refreshBundle() } catch {
                try? await pages.appendLog(namespace, [Self.entry(slug: "", title: "Review", .failed,
                    "The answers were uploaded, but the review questions could not be fetched again: \(error.localizedDescription)")])
            }
        }
        return summary
    }

    private func lockedSync(_ namespace: String) async -> SyncSummary {
        await syncLock.run { () async -> SyncSummary in
            var summary = SyncSummary()
            let waiting: [PendingReviewAnswer]
            do {
                waiting = try await store.pending(namespace)
            } catch {
                summary.logged += await log(namespace, Self.entry(slug: "", title: "Review", .failed,
                    "The review answers waiting to upload cannot be read: \(error.localizedDescription)"))
                return summary
            }
            for start in stride(from: 0, to: waiting.count, by: 500) {
                let batch = Array(waiting[start..<min(start + 500, waiting.count)])
                guard await upload(batch, namespace: namespace, summary: &summary) else { break }
            }
            return summary
        }
    }

    /// One upload; false when the rest should wait (unreachable, signed out, refused).
    private func upload(_ batch: [PendingReviewAnswer], namespace: String, summary: inout SyncSummary) async -> Bool {
        let answer: (status: Int, headers: [String: String], body: Data)
        do {
            answer = try await gateway.fetch("POST", "/api/offline/review/answers", json: .object(["answers": .array(batch.map(\.upload))]))
        } catch .unreachable {
            summary.unreachable = true
            return false
        } catch {
            summary.logged += await log(namespace, Self.entry(slug: "", title: "Review", .failed,
                "Uploading review answers failed: \(error.localizedDescription) They stay on this phone and are tried again."))
            return false
        }
        switch answer.status {
        case 200:
            guard let body = try? JSONValue(data: answer.body), let results = body["results"].array, results.count == batch.count else {
                summary.logged += await log(namespace, Self.entry(slug: "", title: "Review", .failed,
                    "The server's answer to a review upload could not be read. The answers stay on this phone and are tried again."))
                return false
            }
            var entries: [SyncLogEntry] = []
            var schedules: [UUID: (Double, Instant)] = [:]
            for (pending, result) in zip(batch, results) {
                entries += Self.logEntries(for: result, pending: pending)
                if result["status"].string != "rejected" {
                    summary.uploaded += 1
                    let recorded = result["recorded"]
                    if let interval = recorded["interval_days"].number, let due = Instant(isoString: recorded["due_at"].string ?? "") {
                        schedules[pending.clientID] = (interval, due)
                    }
                }
            }
            do {
                try await store.finishUpload(namespace, sent: Set(batch.map(\.clientID)), schedules: schedules)
            } catch {
                entries.append(Self.entry(slug: "", title: "Review", .failed, "The server took the review answers, but the phone "
                    + "could not note it (\(error.localizedDescription)); they are sent again and come back as already accepted."))
            }
            try? await pages.appendLog(namespace, entries)
            summary.logged += entries.count
            return true
        case 401:
            return false // the app asks to sign in again; the answers wait
        default:
            let detail = AuthClient.detail(of: answer.body) ?? "HTTP \(answer.status)"
            summary.logged += await log(namespace, Self.entry(slug: "", title: "Review", .failed,
                "The server refused the review upload (\(answer.status): \(detail)). The answers stay on this phone and are tried again."))
            return false
        }
    }

    private func log(_ namespace: String, _ entry: SyncLogEntry) async -> Int {
        try? await pages.appendLog(namespace, [entry])
        return 1
    }

    static func entry(slug: String, title: String, _ kind: SyncLogEntry.Kind, _ message: String, attempt: String? = nil) -> SyncLogEntry {
        SyncLogEntry(id: UUID(), date: Date(), slug: slug, title: title, kind: kind, message: message, attempt: attempt)
    }

    /// What the reader is told about one uploaded answer (nothing when it simply counted).
    static func logEntries(for result: JSONValue, pending: PendingReviewAnswer) -> [SyncLogEntry] {
        let title = "Review: \(pending.title)"
        if result["status"].string == "rejected" {
            let reason = result["reason"].string ?? "rejected"
            let detail = result["detail"].string ?? ""
            let message: String
            switch reason {
            case "already_answered", "presentation_changed":
                message = "This review was answered elsewhere first (on the web or another device), so the answer made on this "
                    + "phone does not count and the question's schedule follows that answer (\(reason)): \(detail)"
            default:
                message = "The server did not accept this review answer (\(reason)): \(detail)"
            }
            return [entry(slug: pending.slug, title: title, .rejected, message, attempt: pending.summary)]
        }
        if result["verdict_mismatch"].bool == true {
            let phone = result["device_verdict"].string ?? "?"
            let server = result["recorded"]["verdict"].string ?? "?"
            return [entry(slug: pending.slug, title: title, .mismatch, "The server graded this review \(server); the phone had "
                + "graded it \(phone). The server's grade is the one kept.", attempt: pending.summary)]
        }
        return []
    }
}

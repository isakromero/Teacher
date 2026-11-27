import Foundation

/// The highlight endpoints of saved pages (`teacher.web.routers.highlights`): answered on the
/// phone when the server cannot be reached, or before the network while the page has highlight
/// changes waiting to upload (they are uploaded first, as answers are); and the upload of those
/// changes (`POST /api/offline/artifacts/{slug}/highlights`), whose answer replaces the saved list.
extension OfflineService {
    // MARK: Answering

    func highlightReplyBeforeNetwork(_ route: HighlightEngine.Route, request: GatewayRequest, namespace: String) async
        -> GatewayReply? {
        guard let page = await store.page(namespace, route.slug), !page.highlights.changes.isEmpty else { return nil }
        _ = await syncHighlights(slugs: [route.slug])
        guard let after = await store.page(namespace, route.slug), !after.highlights.changes.isEmpty else { return nil }
        return await answerHighlightLocally(route, request: request, namespace: namespace)
    }

    /// Answer a highlight request from the saved list; every change is on disk before the reply.
    func answerHighlightLocally(_ route: HighlightEngine.Route, request: GatewayRequest, namespace: String) async -> GatewayReply {
        await highlightLock.run { () async -> GatewayReply in
            guard let page = await store.page(namespace, route.slug) else { return Self.notSaved(route.slug) }
            var engine = page.highlightEngine
            let body = request.body.flatMap { try? JSONValue(data: $0) }
            let reply: GatewayReply
            do {
                switch route {
                case .list:
                    return Self.json(200, try engine.listView())
                case .create:
                    reply = Self.json(201, try engine.create(body, now: clock()).highlight)
                case .update(_, let id):
                    reply = Self.json(200, try engine.updateNote(id: id, body, now: clock()))
                case .delete(_, let id):
                    try engine.delete(id: id, now: clock())
                    reply = GatewayReply(status: 204, headers: [Gateway.sourceHeader: "saved-page"], data: Data(),
                                         source: .local(ReadingEngine.sourceName))
                }
            } catch let error as EngineError {
                return error.reply
            } catch {
                return .jsonError(500, error.localizedDescription, source: .local(ReadingEngine.sourceName))
            }
            do {
                try await store.saveHighlights(namespace, route.slug, engine.saved)
            } catch {
                return .jsonError(500, "The highlight could not be saved on this phone (\(error.localizedDescription)), so nothing "
                    + "was changed. Try again.", source: .local(ReadingEngine.sourceName))
            }
            changed()
            return reply
        }
    }

    // MARK: Uploading

    struct HighlightSyncSummary: Sendable, Equatable {
        var uploaded = 0
        var logged = 0
        var unreachable = false
    }

    /// Upload the highlight changes of `slugs` (or of every saved page), in the order they were
    /// made; tell the reader about every change that did not apply as made.
    func syncHighlights(slugs: [String]? = nil) async -> HighlightSyncSummary {
        guard let namespace else { return HighlightSyncSummary() }
        return await highlightLock.run { () async -> HighlightSyncSummary in
            var summary = HighlightSyncSummary()
            let pages = await store.pages(namespace).filter {
                !$0.highlights.changes.isEmpty && (slugs?.contains($0.record.slug) ?? true)
            }
            for page in pages where !summary.unreachable {
                await uploadHighlights(page, namespace: namespace, summary: &summary)
            }
            if !pages.isEmpty { changed() }
            return summary
        }
    }

    private func uploadHighlights(_ page: SavedPage, namespace: String, summary: inout HighlightSyncSummary) async {
        let slug = page.record.slug
        let batch = Array(page.highlights.changes.prefix(500))
        let answer: (status: Int, headers: [String: String], body: Data)
        do {
            answer = try await gateway.fetch("POST", "/api/offline/artifacts/\(StudyPageRequests.segment(slug))/highlights",
                                             json: .object(["changes": .array(batch.map(\.upload))]))
        } catch .unreachable {
            summary.unreachable = true
            return
        } catch {
            summary.logged += await logHighlights(namespace, [Self.highlightEntry(page, .failed, "Uploading highlight changes failed: "
                + "\(error.localizedDescription) They stay on this phone and are tried again.")])
            return
        }
        let sent = Set(batch.map(\.changeID))
        switch answer.status {
        case 200:
            guard let body = try? JSONValue(data: answer.body), let results = body["results"].array, results.count == batch.count,
                  let list = body["highlights"].array else {
                summary.logged += await logHighlights(namespace, [Self.highlightEntry(page, .failed, "The server's answer to a highlight "
                    + "upload could not be read. The changes stay on this phone and are tried again.")])
                return
            }
            var entries: [SyncLogEntry] = []
            for (change, result) in zip(batch, results) {
                if let entry = Self.highlightLogEntry(for: result, change: change, page: page) { entries.append(entry) }
                if result["status"].string == "applied" { summary.uploaded += 1 }
            }
            do {
                try await store.finishHighlightUpload(namespace, slug, sent: sent, serverList: list)
            } catch {
                entries.append(Self.highlightEntry(page, .failed, "The server took the highlight changes, but the phone could not "
                    + "note it (\(error.localizedDescription)); they are sent again and come back unchanged."))
            }
            summary.logged += await logHighlights(namespace, entries)
        case 404:
            let entries = batch.map { Self.highlightEntry(page, .deleted, "The page was deleted on the server, so this highlight "
                + "change can never be uploaded. It was removed from the queue.", attempt: Self.describe($0)) }
            try? await store.finishHighlightUpload(namespace, slug, sent: sent, serverList: nil)
            try? await store.markDeletedOnServer(namespace, slug)
            summary.logged += await logHighlights(namespace, entries)
        case 401:
            return // the app asks to sign in again; the changes wait
        default:
            let detail = AuthClient.detail(of: answer.body) ?? "HTTP \(answer.status)"
            summary.logged += await logHighlights(namespace, [Self.highlightEntry(page, .failed, "The server refused the "
                + "highlight upload (\(answer.status): \(detail)). The changes stay on this phone and are tried again.")])
        }
    }

    private func logHighlights(_ namespace: String, _ entries: [SyncLogEntry]) async -> Int {
        try? await store.appendLog(namespace, entries)
        return entries.count
    }

    /// What the reader is told about one uploaded change: nothing when it applied (or changed
    /// nothing because the server already had it), a line otherwise.
    static func highlightLogEntry(for result: JSONValue, change: HighlightChange, page: SavedPage) -> SyncLogEntry? {
        let what = describe(change)
        switch result["status"].string {
        case "applied", "unchanged":
            return nil
        case "stale":
            return highlightEntry(page, .mismatch, "This note was changed later elsewhere (on the web page or another device), "
                + "so the newer note was kept and the one written on this phone was not applied.", attempt: what)
        case "deleted":
            return highlightEntry(page, .mismatch, "This highlight was removed elsewhere, so the note written on this phone was "
                + "not applied.", attempt: what)
        default:
            let reason = result["reason"].string ?? "rejected"
            return highlightEntry(page, .rejected, "The server did not accept this highlight change (\(reason)): "
                + (result["detail"].string ?? ""), attempt: what)
        }
    }

    static func highlightEntry(_ page: SavedPage, _ kind: SyncLogEntry.Kind, _ message: String, attempt: String? = nil)
        -> SyncLogEntry {
        SyncLogEntry(id: UUID(), date: Date(), slug: page.record.slug, title: page.record.title, kind: kind, message: message,
                     attempt: attempt)
    }

    static func describe(_ change: HighlightChange) -> String {
        let quote = change.quote.count > 80 ? String(change.quote.prefix(80)) + "…" : change.quote
        let action = switch change.op {
        case "create": "Highlighted"
        case "update_note": "Note changed on"
        default: "Removed"
        }
        return "\(action) “\(quote)”, \(change.madeAt.date.formatted(date: .abbreviated, time: .shortened))"
    }
}

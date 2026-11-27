import Foundation

/// What a study page's replies said about its progress, read on the way to the page (as
/// `GradedAnswer` reads grades), so the app can tell when the reader finished a page.
///
/// The server's notion of a finished page is `progress.complete` (`teacher.web.routers.artifacts`:
/// every gate cleared, `teacher.reading.ReadingState.complete`). The web client (`static/js/reader.js`)
/// learns it from two replies, which carry it as `complete`:
/// - `GET /api/artifacts/{slug}/blocks` (`routers.reading.list_blocks`), as the page opens;
/// - `POST /api/artifacts/{slug}/blocks/{key}/responses` 201 (`routers.reading.create_response`),
///   after each answer.
/// A saved page answered on the phone gets both from `ReadingEngine` in the same shape
/// (`blocksView`, `reply`), so the same reading covers it. The title comes from
/// `GET /api/artifacts/{slug}` (`ArtifactOut.title`), which the page loads first.
struct ReadingReport: Sendable, Equatable {
    enum Kind: Sendable, Equatable {
        /// The page's blocks were loaded: whether it was complete then.
        case state(complete: Bool)
        /// An answer was recorded: whether the page is complete after it.
        case answered(complete: Bool)
        /// The page's title.
        case title(String)
    }

    /// The server and account (`GatewayConnection.cacheNamespace`).
    var namespace: String
    var slug: String
    var kind: Kind
}

enum ReadingProgress {
    /// The requests whose replies are read.
    enum Watched: Equatable {
        case blocks(slug: String)
        case submit(slug: String)
        case artifact(slug: String)
    }

    /// Replies larger than this are not read (a blocks list is tens of kilobytes).
    static let maxBody = 4 << 20

    static func watched(_ request: GatewayRequest) -> Watched? {
        switch OfflineService.readingRoute(request) {
        case .blocks(let slug): return .blocks(slug: slug)
        case .submit(let slug, _): return .submit(slug: slug)
        case .responses: return nil
        case nil: break
        }
        guard request.method == "GET" else { return nil }
        let parts = request.path.split(separator: "/", omittingEmptySubsequences: false)
        // "", "api", "artifacts", slug
        guard parts.count == 4, parts[0].isEmpty, parts[1] == "api", parts[2] == "artifacts", !parts[3].isEmpty,
              let slug = String(parts[3]).removingPercentEncoding else { return nil }
        return .artifact(slug: slug)
    }

    /// Where the reply came from decides whether it says anything about the page now: the
    /// server and the phone's reading engine do; a copy saved earlier (`cache`) is only trusted
    /// for the title, and the gateway's own errors for nothing.
    static func report(_ watched: Watched, namespace: String, status: Int, source: ReplySource, body: Data) -> ReadingReport? {
        let current = switch source {
        case .server, .local: true
        case .cache, .gatewayError: false
        }
        guard let value = try? JSONValue(data: body), value.object != nil else { return nil }
        switch watched {
        case .blocks(let slug):
            guard current, status == 200, let complete = value["complete"].bool else { return nil }
            return ReadingReport(namespace: namespace, slug: slug, kind: .state(complete: complete))
        case .submit(let slug):
            guard current, status == 201, let complete = value["complete"].bool else { return nil }
            return ReadingReport(namespace: namespace, slug: slug, kind: .answered(complete: complete))
        case .artifact(let slug):
            guard source != .gatewayError, status == 200, value["slug"].string == slug,
                  let title = value["title"].string, !title.isEmpty else { return nil }
            return ReadingReport(namespace: namespace, slug: slug, kind: .title(title))
        }
    }

    /// `reply` with its body passed through unchanged; once the page has read it, what it says
    /// about the page's progress (if anything) is given to `report`.
    static func watch(_ reply: GatewayReply, _ watched: Watched, namespace: String,
                      report: @escaping @Sendable (ReadingReport) -> Void) -> GatewayReply {
        guard (200...299).contains(reply.status) else { return reply }
        let status = reply.status
        let source = reply.source
        return reply.watchingBody(maxBody: maxBody) { body in
            if let found = Self.report(watched, namespace: namespace, status: status, source: source, body: body) { report(found) }
        }
    }
}

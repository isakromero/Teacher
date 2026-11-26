import Foundation

/// How a checkpoint answer was graded, read from the reply to its `POST .../responses`.
enum AnswerOutcome: String, Sendable, Equatable {
    case passed
    /// Part of what was asked is there (`verdict` "partial"); not a pass.
    case partial
    case failed
}

/// Recognises the replies that grade an answer, so the app can give a haptic for them without
/// the web client's help.
///
/// The requests: `POST /api/artifacts/{slug}/blocks/{key}/responses` (a page's checkpoint,
/// `teacher.web.routers.reading.create_response`, answered by the server or by the phone's
/// `ReadingEngine`) and `POST /api/artifacts/{slug}/chat/blocks/{id}/responses` (an exercise the
/// tutor set, `routers.chat.create_block_response`). Both answer 201 with `passed` (a bool)
/// and `verdict` ("passed", "partial" or "failed"; `passed` is true exactly for "passed",
/// `teacher.grading`). Anything else (another status, a body that is not that object, the two
/// fields missing or disagreeing) is not classified: no haptic rather than a wrong one.
enum GradedAnswer {
    /// Replies larger than this are not read for a grade (a grade is a few kilobytes).
    static let maxBody = 1 << 20

    static func isSubmission(_ request: GatewayRequest) -> Bool {
        guard request.method == "POST" else { return false }
        let parts = request.path.split(separator: "/", omittingEmptySubsequences: false)
        // "", "api", "artifacts", slug, ["chat",] "blocks", key, "responses"
        guard parts.count >= 7, parts[0].isEmpty, parts[1] == "api", parts[2] == "artifacts", !parts[3].isEmpty,
              parts.last == "responses", !parts[parts.count - 2].isEmpty else { return false }
        switch parts.count {
        case 7: return parts[4] == "blocks"
        case 8: return parts[4] == "chat" && parts[5] == "blocks"
        default: return false
        }
    }

    static func outcome(status: Int, body: Data) -> AnswerOutcome? {
        guard status == 201, let value = try? JSONValue(data: body), value.object != nil,
              let passed = value["passed"].bool,
              let verdict = value["verdict"].string.flatMap(AnswerOutcome.init(rawValue:)),
              passed == (verdict == .passed)
        else { return nil }
        return verdict
    }

    /// `reply` with its body passed through unchanged; once it has been read to the end, the
    /// grade it holds (if any) is given to `graded`. A body that broke off or was not read to
    /// the end gives nothing.
    static func watch(_ reply: GatewayReply, graded: @escaping @Sendable (AnswerOutcome) -> Void) -> GatewayReply {
        guard reply.status == 201 else { return reply }
        let body = reply.body
        let status = reply.status
        var watched = reply
        watched.body = AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var collected = Data()
                    var tooLarge = false
                    for try await chunk in body {
                        try Task.checkCancellation()
                        if !tooLarge {
                            collected.append(chunk)
                            if collected.count > maxBody {
                                tooLarge = true
                                collected = Data()
                            }
                        }
                        continuation.yield(chunk)
                    }
                    // The whole reply has been handed on; reported before the stream ends, so
                    // whoever read the reply to its end sees the grade reported.
                    if !tooLarge, let outcome = outcome(status: status, body: collected) { graded(outcome) }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { termination in
                if case .cancelled = termination { task.cancel() }
            }
        }
        return watched
    }
}

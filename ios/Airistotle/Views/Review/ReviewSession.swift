import Foundation
import Observation

/// One review session: the questions one at a time, from the server while it can be reached and
/// from the questions saved on the phone when it cannot (graded here, uploaded later).
@MainActor
@Observable
final class ReviewSession {
    enum Source: Equatable {
        case server
        /// The saved queue, graded on the phone; `reason` says why.
        case phone(reason: String)
    }

    enum Phase: Equatable {
        case loading
        case asking(ReviewItem)
        case graded(ReviewItem, ReviewResult)
        /// Nothing to ask when the session started.
        case nothingDue(text: String, canExtend: Bool)
        case summary
        /// The session could not start (not a connection problem: that falls back to the phone).
        case failed(String)
    }

    /// One answer of the session, for the summary.
    struct Answered: Equatable {
        var itemID: Int
        var verdict: String
        var onPhone: Bool
    }

    let request: ReviewRequest
    private(set) var phase: Phase = .loading
    private(set) var source: Source = .server
    private(set) var answered: [Answered] = []
    /// The number of questions the session expects (it grows if more come due).
    private(set) var total = 0
    /// Why the last submission was not graded (shown with the question; nothing was recorded).
    private(set) var problem: String?
    /// The question can only be skipped (it cannot be answered now).
    private(set) var problemSkippable = false
    private(set) var submitting = false
    private(set) var timeBox: TimeBox?
    /// For the summary: when the next question is due, and what else to say.
    private(set) var nextDue: Date?
    private(set) var summaryNote: String?
    /// A new value for each verdict (the haptic).
    private(set) var lastVerdict: Verdict?

    struct Verdict: Equatable {
        let id = UUID()
        let outcome: AnswerOutcome
    }

    // The answer being written.
    var choice: Int?
    var text = ""
    var confidence: ReviewConfidence?

    private let review: ReviewService
    private let now: () -> Date
    private var cap = ReviewService.defaultCap
    private var skipped = Set<Int>()
    private var timeUpEnded = false

    init(request: ReviewRequest, review: ReviewService, offline: Bool, now: @escaping () -> Date = Date.init) {
        self.request = request
        self.review = review
        self.now = now
        if offline { source = .phone(reason: "The server cannot be reached.") }
        if case .quick(let minutes) = request.kind { timeBox = TimeBox(start: now(), minutes: minutes) }
    }

    var isQuick: Bool { timeBox != nil }
    /// The question on screen is number `position` (a graded one is already counted in `answered`).
    var position: Int {
        switch phase {
        case .asking: answered.count + 1
        case .graded: answered.count
        default: answered.count
        }
    }

    var onPhone: Bool { if case .phone = source { true } else { false } }

    var currentItem: ReviewItem? {
        switch phase {
        case .asking(let item), .graded(let item, _): item
        default: nil
        }
    }

    // MARK: Starting

    func start() async {
        if case .phone(let reason) = source {
            await startOnPhone(reason: reason)
            return
        }
        do {
            let page = try await review.queue(cap: cap, limit: 1)
            guard let first = page.items.first else {
                phase = .nothingDue(text: ReviewText.nothingDue(itemsTotal: page.itemsTotal, due: page.due,
                                                                remainingToday: page.remainingToday, cap: page.cap,
                                                                nextDue: page.nextDueAt?.date, now: now()),
                                    canExtend: page.due > 0 && page.remainingToday == 0 && cap < 500)
                return
            }
            total = max(min(page.due, page.remainingToday), 1)
            ask(first)
        } catch .unreachable(let reason) {
            await startOnPhone(reason: "The server cannot be reached (\(reason)).")
        } catch {
            phase = .failed("The review questions could not be loaded: \(error.localizedDescription)")
        }
    }

    private func startOnPhone(reason: String) async {
        source = .phone(reason: reason)
        let queue: LocalReviewQueue?
        do {
            queue = try await review.localQueue()
        } catch {
            phase = .failed("The review questions saved on this phone cannot be read: \(error.localizedDescription)")
            return
        }
        guard let queue else {
            phase = .failed("\(reason) No review questions are saved on this phone yet: they are saved whenever the app "
                + "can reach the server.")
            return
        }
        let moment = review.now()
        let askable = queue.askable(now: moment, concept: request.concept).filter { !skipped.contains($0.id) }
        guard let first = askable.first else {
            let due = queue.due(now: moment, concept: request.concept).count
            phase = .nothingDue(text: ReviewText.nothingDue(itemsTotal: queue.bundle.items.count, due: due,
                                                            remainingToday: queue.remaining(now: moment), cap: queue.bundle.cap,
                                                            nextDue: queue.nextDue(after: moment)?.date, now: now()),
                                canExtend: false)
            return
        }
        total = max(total, answered.count + askable.count)
        ask(first)
    }

    /// "Keep going" after today's cap: ten more (as the web page's button).
    func keepGoing() async {
        cap = min(cap + 10, 500)
        phase = .loading
        await start()
    }

    private func ask(_ item: ReviewItem) {
        choice = nil
        text = ""
        confidence = nil
        problem = nil
        problemSkippable = false
        phase = .asking(item)
    }

    // MARK: Answering

    /// Whether the answer is complete enough to send (the word count is the server's rule, checked on submit).
    var canSubmit: Bool {
        guard case .asking(let item) = phase, !submitting, confidence != nil else { return false }
        return item.type == "blurb" ? !Python.isBlank(text) : choice != nil
    }

    var response: JSONValue? {
        guard let item = currentItem else { return nil }
        if item.type == "blurb" { return .object(["text": .string(text)]) }
        return choice.map { .object(["choice": .int($0)]) }
    }

    func submit() async {
        guard case .asking(let item) = phase, let response, !submitting else { return }
        submitting = true
        defer { submitting = false }
        problem = nil
        problemSkippable = false
        switch source {
        case .server:
            do {
                let result = try await review.answerOnline(item, response: response, confidence: confidence ?? .medium)
                graded(item, result, onPhone: false)
            } catch .unreachable(let reason) {
                source = .phone(reason: "The server could not be reached (\(reason)).")
                await submitOnPhone(item, response: response, afterFailure: true)
            } catch .refused(let status, let detail) where status == 409 {
                problem = "This question cannot be answered any more: \(detail) Nothing was recorded."
                problemSkippable = true
            } catch {
                problem = error.localizedDescription
            }
        case .phone:
            await submitOnPhone(item, response: response, afterFailure: false)
        }
    }

    private func submitOnPhone(_ item: ReviewItem, response: JSONValue, afterFailure: Bool) async {
        do {
            let result = try await review.answerOffline(item, response: response, confidence: confidence)
            graded(item, result, onPhone: true)
        } catch .notSaved(let detail) {
            problem = afterFailure ? "The server could not be reached, so the answer was not sent. \(detail)" : detail
            problemSkippable = true
        } catch .grading(let detail) {
            problem = detail
            problemSkippable = true
        } catch {
            problem = error.localizedDescription
        }
    }

    private func graded(_ item: ReviewItem, _ result: ReviewResult, onPhone: Bool) {
        answered.append(Answered(itemID: item.id, verdict: result.verdict, onPhone: onPhone))
        lastVerdict = Verdict(outcome: result.outcome)
        phase = .graded(item, result)
    }

    // MARK: Moving on

    /// What the button after a verdict does now.
    var stepAfterVerdict: ReviewStep {
        ReviewStep.afterVerdict(timeBox: timeBox, now: now(), moreLeft: answered.count < total)
    }

    /// Leave a question that cannot be answered now (it stays due).
    func skip() async {
        if let item = currentItem { skipped.insert(item.id) }
        await next()
    }

    func next() async {
        if ReviewStep.afterVerdict(timeBox: timeBox, now: now(), moreLeft: true) == .finish {
            timeUpEnded = true
            await finish()
            return
        }
        if answered.count >= total {
            await finish() // every question the session expected is answered
            return
        }
        phase = .loading
        if case .phone(let reason) = source {
            await nextOnPhone(reason: reason)
            return
        }
        do {
            let page = try await review.queue(cap: cap, limit: min(skipped.count + 1, 50))
            guard let item = page.items.first(where: { !skipped.contains($0.id) }) else {
                await finish()
                return
            }
            total = max(total, answered.count + min(page.due, page.remainingToday))
            ask(item)
        } catch .unreachable(let reason) {
            await nextOnPhone(reason: "The server cannot be reached (\(reason)).")
        } catch {
            phase = .failed("The next question could not be loaded: \(error.localizedDescription)")
        }
    }

    private func nextOnPhone(reason: String) async {
        source = .phone(reason: reason)
        let moment = review.now()
        guard let queue = try? await review.localQueue(),
              let item = queue.askable(now: moment, concept: request.concept).first(where: { !skipped.contains($0.id) }) else {
            await finish()
            return
        }
        ask(item)
    }

    /// The summary: what was answered and when the next question is due.
    func finish() async {
        phase = .summary
        let moment = review.now()
        if case .server = source {
            do {
                let page = try await review.queue(cap: cap, limit: 0)
                nextDue = page.nextDueAt?.date
                if page.due > 0 {
                    summaryNote = page.remainingToday == 0
                        ? "\(page.due) more due, but today's \(page.cap) reviews are done."
                        : "\(page.due) more due now."
                } else {
                    summaryNote = nil
                }
                return
            } catch .unreachable {
                // fall through to the saved queue
            } catch {
                summaryNote = "When the next question is due could not be loaded: \(error.localizedDescription)"
                return
            }
        }
        let queue = try? await review.localQueue()
        nextDue = queue?.nextDue(after: moment)?.date
        let left = queue?.askable(now: moment, concept: request.concept).filter { !skipped.contains($0.id) }.count ?? 0
        summaryNote = left > 0 ? "\(left) more saved on this phone are due now." : nil
    }

    var endedByTime: Bool { timeUpEnded }
    var passedCount: Int { answered.filter { $0.verdict == "passed" }.count }
    var partialCount: Int { answered.filter { $0.verdict == "partial" }.count }
    var failedCount: Int { answered.filter { $0.verdict == "failed" }.count }
    var onPhoneCount: Int { answered.filter(\.onPhone).count }
}

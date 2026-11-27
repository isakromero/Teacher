import Foundation

/// The review queue saved on the phone (`GET /api/offline/review`, see
/// `teacher.web.routers.offline_review`), kept as the server's JSON.
struct ReviewBundle: Sendable, Equatable {
    static let format = "teacher-offline-review-bundle"
    static let formatVersion = 1

    var raw: JSONValue

    init(_ raw: JSONValue) throws(OfflineBundle.Invalid) {
        guard raw["format"].string == Self.format else {
            throw .init(message: "The server's answer is not a review bundle (format \(raw["format"])).")
        }
        guard raw["format_version"].int == Self.formatVersion else {
            throw .init(message: "The server sends review bundles of version \(raw["format_version"]), "
                + "but this app reads version \(Self.formatVersion). Update the app.")
        }
        for key in ["bundled_at", "tz", "day_start"] where raw[key].string == nil {
            throw .init(message: "The review bundle has no \(key).")
        }
        guard Instant(isoString: raw["bundled_at"].string ?? "") != nil, Instant(isoString: raw["day_start"].string ?? "") != nil,
              TimeZone(identifier: raw["tz"].string ?? "") != nil else {
            throw .init(message: "The review bundle's times or time zone cannot be read.")
        }
        guard raw["cap"].int != nil, raw["remaining_today"].int != nil, raw["default_min_words"].int != nil,
              raw["grading"].object != nil, raw["items"].array != nil, ReviewSchedule(raw["schedule"]) != nil else {
            throw .init(message: "The review bundle is missing fields this app needs.")
        }
        for item in raw["items"].array ?? [] where ReviewItem(item) == nil {
            throw .init(message: "A question in the review bundle cannot be read (item \(item["id"])).")
        }
        self.raw = raw
    }

    init(data: Data) throws(OfflineBundle.Invalid) {
        let value: JSONValue
        do {
            value = try JSONValue(data: data)
        } catch {
            throw .init(message: "The review bundle could not be read: \(error.localizedDescription)")
        }
        try self.init(value)
    }

    // The initialiser checked every field these read.
    var bundledAt: Instant { Instant(isoString: raw["bundled_at"].string ?? "") ?? Instant(micros: 0) }
    var dayStart: Instant { Instant(isoString: raw["day_start"].string ?? "") ?? Instant(micros: 0) }
    var timeZone: TimeZone { TimeZone(identifier: raw["tz"].string ?? "") ?? .gmt }
    var cap: Int { raw["cap"].int ?? 0 }
    var remainingToday: Int { raw["remaining_today"].int ?? 0 }
    var defaultMinWords: Int { raw["default_min_words"].int ?? 20 }
    var grading: JSONValue { raw["grading"] }
    var schedule: ReviewSchedule { ReviewSchedule(raw["schedule"]) ?? ReviewSchedule(first: 1, growth: 2.2, max: 180) }
    var items: [ReviewItem] { (raw["items"].array ?? []).compactMap(ReviewItem.init) }
}

/// `teacher.review_schedule`: the interval after a review, as the bundle states it.
struct ReviewSchedule: Sendable, Equatable {
    var first: Double
    var growth: Double
    var max: Double

    init(first: Double, growth: Double, max: Double) {
        (self.first, self.growth, self.max) = (first, growth, max)
    }

    init?(_ value: JSONValue) {
        guard let first = value["first_interval_days"].number, let growth = value["growth"].number,
              let max = value["max_interval_days"].number else { return nil }
        self.init(first: first, growth: growth, max: max)
    }

    /// passed: interval × growth (at most max); failed: the first interval; partial: unchanged.
    func interval(after verdict: String, from interval: Double) -> Double {
        switch verdict {
        case "passed": Swift.min(interval * growth, max)
        case "failed": first
        default: interval
        }
    }
}

/// One question of the review bundle (or of `GET /api/review/queue`, which has the same fields
/// without `private`, `due_now` and `last_answered_at`).
struct ReviewItem: Sendable, Equatable, Identifiable {
    var raw: JSONValue

    init?(_ raw: JSONValue) {
        guard raw["id"].int != nil, raw["presentation"].string != nil, raw["block"]["type"].string != nil,
              raw["block"]["content"].object != nil, raw["artifact"]["slug"].string != nil,
              Instant(isoString: raw["due_at"].string ?? "") != nil, raw["interval_days"].number != nil else { return nil }
        self.raw = raw
    }

    var id: Int { raw["id"].int ?? 0 }
    var presentation: String { raw["presentation"].string ?? "" }
    var type: String { raw["block"]["type"].string ?? "" }
    var block: JSONValue { raw["block"] }
    var content: JSONValue { raw["block"]["content"] }
    /// The answer key in the form and order shown (bundle items only; null in the online queue).
    var answerKey: JSONValue { raw["private"] }
    var artifactSlug: String { raw["artifact"]["slug"].string ?? "" }
    var artifactTitle: String { raw["artifact"]["title"].string ?? artifactSlug }
    var dueAt: Instant { Instant(isoString: raw["due_at"].string ?? "") ?? Instant(micros: 0) }
    var intervalDays: Double { raw["interval_days"].number ?? 1 }
    var confidentError: Bool { raw["confident_error"].bool == true }
    var reviews: Int { raw["reviews"].int ?? 0 }
    var dueWhenBundled: Bool { raw["due_now"].bool == true }
    var conceptNames: [String] { (raw["concepts"].array ?? []).compactMap { $0["name"].string } }
    var conceptSlugs: [String] { (raw["concepts"].array ?? []).compactMap { $0["slug"].string } }
    /// The question's text: a multiple choice's `question`, a blurb's `prompt` (Markdown with maths).
    var prompt: String { content["question"].string ?? content["prompt"].string ?? "" }
    var options: [String] { (content["options"].array ?? []).compactMap(\.string) }
    var minWords: Int? { content["min_words"].int }

    /// The label of the form asked (`review_model.js`'s `formLabel`).
    var formLabel: String {
        if raw["form"].string == "variant" { return "A fresh form of a question from the page (variant \(raw["variant"].int ?? 0))" }
        return raw["variants_available"].int ?? 0 > 0 ? "The question as it is on the page"
            : "The question as it is on the page (this page has no variants)"
    }

    /// Why it is where it is in the queue, if worth saying (`itemNote`).
    var note: String? {
        if confidentError { return "You said you were certain last time, and it was not right: this one comes first." }
        if reviews == 0 { return "First review since you answered it on the page." }
        return nil
    }
}

/// How sure the reader is: the server requires one of these for a review.
enum ReviewConfidence: String, CaseIterable, Sendable, Identifiable, Codable {
    case low, medium, high

    var id: Self { self }

    /// The web gate's labels (`gate_model.js`).
    var label: String {
        switch self {
        case .low: "Guessing"
        case .medium: "Fairly sure"
        case .high: "Certain"
        }
    }
}

/// Checking and grading a review answer on the phone, as `teacher.web.routers.offline_review`
/// says ("Grading on the phone").
enum ReviewGrading {
    static let deviceRulesGrader = OfflineService.choiceGrader

    /// A checked answer: nothing is graded or recorded yet.
    struct Answer: Sendable, Equatable {
        var item: ReviewItem
        var response: JSONValue
        var confidence: ReviewConfidence
        /// For a blurb: the text.
        var text: String?
    }

    /// The server's 422s for a malformed answer (`teacher.grading._choice`,
    /// `teacher.judge._answer_text`); nothing counts as an attempt.
    static func check(_ item: ReviewItem, response: JSONValue, confidence: ReviewConfidence?,
                      defaultMinWords: Int) throws(EngineError) -> Answer {
        guard let confidence else {
            throw EngineError(status: 422, detail: "Say how sure you are first: Guessing, Fairly sure or Certain.")
        }
        guard case .object(let fields) = response else {
            throw EngineError(status: 422, detail: "response must be an object, got \(response.pythonTypeName)")
        }
        switch item.type {
        case "multiple_choice":
            guard fields.keys.sorted() == ["choice"] else {
                throw EngineError(status: 422, detail: "response must be exactly {\"choice\": <option>}")
            }
            guard case .int(let choice) = fields["choice"] ?? .null else {
                throw EngineError(status: 422, detail: "choice must be an integer option index")
            }
            let count = item.options.count
            guard choice >= 0, choice < count else {
                throw EngineError(status: 422, detail: "choice must be between 0 and \(count - 1), got \(choice)")
            }
            return Answer(item: item, response: response, confidence: confidence)
        case "blurb":
            guard fields.keys.sorted() == ["text"], case .string(let text) = fields["text"] ?? .null else {
                throw EngineError(status: 422, detail: "text must be a string")
            }
            guard !Python.isBlank(text) else { throw EngineError(status: 422, detail: "text must not be empty") }
            let minWords = item.minWords ?? defaultMinWords
            let words = Python.words(text).count
            if words < minWords {
                throw EngineError(status: 422, detail: "Write at least \(minWords) words before submitting (you wrote \(words)); "
                    + "this was not counted as an attempt.")
            }
            if (item.answerKey["rubric"].array ?? []).isEmpty {
                throw EngineError(status: 422, detail: "blurb rubric is empty; there is nothing to grade against")
            }
            return Answer(item: item, response: response, confidence: confidence, text: text)
        default:
            throw EngineError(status: 422, detail: "a \(item.type) cannot be reviewed")
        }
    }

    /// A multiple choice by its key (nil for a blurb, which the on-device judge grades).
    static func choiceGrade(_ answer: Answer, grading: JSONValue) -> Grade? {
        guard answer.item.type == "multiple_choice", let choice = answer.response["choice"].int else { return nil }
        let key = answer.item.answerKey
        if choice == key["correct_index"].int {
            return Grade(verdict: "passed", feedback: key["explanation"].string, missing: [], details: .null,
                         grader: deviceRulesGrader)
        }
        guard let explanations = key["option_explanations"].array else {
            return Grade(verdict: "failed", feedback: grading["no_option_feedback"].string, missing: [], details: .null,
                         grader: deviceRulesGrader)
        }
        let explanation = choice < explanations.count ? explanations[choice].string ?? "" : ""
        return Grade(verdict: "failed", feedback: (grading["multiple_choice_wrong_prefix"].string ?? "") + explanation,
                     missing: [], details: .null, grader: deviceRulesGrader)
    }

    /// The item as a study-page block for `BlurbGrader` (its content and answer key).
    static func judgeBlock(_ item: ReviewItem) -> JSONValue {
        .object(["content": item.content, "private": item.answerKey])
    }

    /// The upload item of `POST /api/offline/review/answers` for an answer graded on the phone.
    static func upload(_ answer: Answer, grade: Grade, clientID: UUID, madeAt: Instant) -> JSONValue {
        .object([
            "client_id": .string(clientID.uuidString.lowercased()),
            "item_id": .int(answer.item.id),
            "presentation": .string(answer.item.presentation),
            "response": answer.response,
            "confidence": .string(answer.confidence.rawValue),
            "verdict": .string(grade.verdict),
            "feedback": grade.feedback.map(JSONValue.string) ?? .null,
            "missing": .array(grade.missing.map(JSONValue.string)),
            "details": grade.details.object == nil ? .null : grade.details,
            "grader": .string(grade.grader.hasPrefix("device:") ? grade.grader : deviceRulesGrader),
            "made_at": .string(madeAt.iso)
        ])
    }
}

/// A review answered on this phone (online or offline) since the saved bundle was made: the
/// question leaves the saved queue (its token is used up) and counts against the day's cap.
struct LocalReviewAnswer: Codable, Sendable, Equatable {
    var itemID: Int
    var presentation: String
    var slug: String
    var madeAt: Instant
    var verdict: String
    var intervalDays: Double
    /// When the question is next due after this answer (the server's when it said so).
    var dueAt: Instant
    /// The answer still waits to upload (`PendingReviewAnswer`).
    var pending: Bool
}

/// The saved queue on the phone's clock: what is due, in which order, how many more may be
/// answered today, and when the next question comes due.
struct LocalReviewQueue: Sendable, Equatable {
    var bundle: ReviewBundle
    var answered: [LocalReviewAnswer]

    private var answeredIDs: Set<Int> { Set(answered.map(\.itemID)) }

    /// The start of the day containing `now` in the bundle's time zone.
    func dayStart(of now: Instant) -> Instant {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = bundle.timeZone
        return Instant(calendar.startOfDay(for: now.date))
    }

    /// Answers still allowed on the day of `now`: today's `remaining_today` less the answers made
    /// here that the bundle does not count; on a later day, `cap` less that day's answers.
    func remaining(now: Instant) -> Int {
        let start = dayStart(of: now)
        let end = start.adding(days: 1)
        let made = answered.filter { $0.madeAt >= start && $0.madeAt < end }.count
        let allowed = start <= bundle.dayStart ? bundle.remainingToday : bundle.cap
        return Swift.max(allowed - made, 0)
    }

    /// Every unanswered question due at `now` (optionally only those about `concept`), in the
    /// order to ask them: those due when bundled in the bundle's order, then the ones that came
    /// due since, confident errors first, then the most overdue, interleaved by page.
    func due(now: Instant, concept: String? = nil) -> [ReviewItem] {
        let skip = answeredIDs
        let open = bundle.items.filter { !skip.contains($0.id) && $0.dueAt <= now && (concept.map($0.conceptSlugs.contains) ?? true) }
        let first = open.filter(\.dueWhenBundled)
        let later = open.filter { !$0.dueWhenBundled }.sorted {
            ($0.confidentError ? 0 : 1, $0.dueAt) < ($1.confidentError ? 0 : 1, $1.dueAt)
        }
        let previous = first.last?.artifactSlug ?? answered.max { $0.madeAt < $1.madeAt }?.slug
        return first + Self.interleave(later, previous: previous)
    }

    /// The questions to ask now: those due, as many as today's cap still allows.
    func askable(now: Instant, concept: String? = nil) -> [ReviewItem] {
        Array(due(now: now, concept: concept).prefix(remaining(now: now)))
    }

    /// `teacher.review_schedule.interleave`.
    static func interleave(_ items: [ReviewItem], previous: String?) -> [ReviewItem] {
        var left = items
        var out: [ReviewItem] = []
        var last = previous
        while !left.isEmpty {
            let index = left.firstIndex { $0.artifactSlug != last } ?? 0
            let item = left.remove(at: index)
            out.append(item)
            last = item.artifactSlug
        }
        return out
    }

    /// When the next question comes due after `now`: an unanswered one not due yet, or one
    /// answered here at its new time. Nil when the saved queue has nothing more.
    func nextDue(after now: Instant) -> Instant? {
        upcoming(after: now).first
    }

    /// Every moment after `now` at which a saved question comes due (one per question, soonest
    /// first): the unanswered ones not due yet, and those answered here at their new time.
    func upcoming(after now: Instant) -> [Instant] {
        let skip = answeredIDs
        let waiting = bundle.items.filter { !skip.contains($0.id) && $0.dueAt > now }.map(\.dueAt)
        return (waiting + answered.map(\.dueAt).filter { $0 > now }).sorted()
    }
}

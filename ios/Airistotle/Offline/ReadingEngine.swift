import Foundation

/// An offline bundle (`GET /api/offline/artifacts/{slug}`, see `teacher.web.routers.offline`),
/// kept as the server's JSON so every field (segments, concepts, ...) is passed on unchanged.
struct OfflineBundle: Sendable, Equatable {
    static let format = "teacher-offline-bundle"
    static let formatVersion = 1

    var raw: JSONValue

    struct Invalid: Error, LocalizedError {
        var message: String
        var errorDescription: String? { message }
    }

    init(_ raw: JSONValue) throws(Invalid) {
        guard raw["format"].string == Self.format else {
            throw Invalid(message: "The server's answer is not an offline bundle (format \(raw["format"])).")
        }
        guard raw["format_version"].int == Self.formatVersion else {
            throw Invalid(message: "The server sends offline bundles of version \(raw["format_version"]), "
                + "but this app reads version \(Self.formatVersion). Update the app.")
        }
        for key in ["artifact", "version", "title"] where raw[key].string == nil {
            throw Invalid(message: "The offline bundle has no \(key).")
        }
        guard raw["max_attempts"].int != nil, raw["default_min_words"].int != nil, raw["blocks"].array != nil,
              raw["grading"].object != nil, raw["assets"].array != nil
        else { throw Invalid(message: "The offline bundle is missing fields this app needs.") }
        self.raw = raw
    }

    init(data: Data) throws(Invalid) {
        let value: JSONValue
        do {
            value = try JSONValue(data: data)
        } catch {
            throw Invalid(message: "The offline bundle could not be read: \(error.localizedDescription)")
        }
        try self.init(value)
    }

    var slug: String { raw["artifact"].string ?? "" }
    var title: String { raw["title"].string ?? "" }
    var version: String { raw["version"].string ?? "" }
    var supersededBy: String? { raw["superseded_by"].string }
    var maxAttempts: Int { raw["max_attempts"].int ?? 2 }
    var defaultMinWords: Int { raw["default_min_words"].int ?? 20 }
    var blocks: [JSONValue] { raw["blocks"].array ?? [] }
    var grading: JSONValue { raw["grading"] }

    struct Asset: Sendable, Equatable {
        var path: String
        var url: String
        var bytes: Int
        var sha256: String
    }

    var assets: [Asset] {
        (raw["assets"].array ?? []).compactMap { item in
            guard let path = item["path"].string, let url = item["url"].string,
                  let bytes = item["bytes"].int, let sha = item["sha256"].string else { return nil }
            return Asset(path: path, url: url, bytes: bytes, sha256: sha)
        }
    }

    /// The bundle with `rows` appended to the responses of their blocks (attempts made on the
    /// phone and not yet uploaded).
    func adding(_ rows: [(key: String, row: JSONValue)]) -> OfflineBundle {
        guard !rows.isEmpty, case .object(var top) = raw, case .array(var blocks) = raw["blocks"] else { return self }
        for (key, row) in rows {
            guard let index = blocks.firstIndex(where: { $0["key"].string == key }),
                  case .object(var block) = blocks[index] else { continue }
            block["responses"] = .array((block["responses"]?.array ?? []) + [row])
            blocks[index] = .object(block)
        }
        top["blocks"] = .array(blocks)
        var copy = self
        copy.raw = .object(top)
        return copy
    }
}

/// An error the reading endpoints would answer with: the status and the server's `detail`.
struct EngineError: Error, Equatable, LocalizedError {
    var status: Int
    var detail: String
    var errorDescription: String? { detail }

    var reply: GatewayReply { .jsonError(status, detail, source: .local(ReadingEngine.sourceName)) }
}

/// A grade, as a grader gives it (`teacher.grading.GradeResult`).
struct Grade: Sendable, Equatable {
    var verdict: String
    var feedback: String?
    var missing: [String]
    var details: JSONValue
    var grader: String
}

/// The reading endpoints of a saved page, computed on the phone from its bundle exactly as
/// the module docstring of `teacher.web.routers.offline` says (the port of the reference
/// `Phone` in `tests/web/test_offline.py`): `GET .../blocks`, `GET .../blocks/{key}/responses`
/// and `POST .../blocks/{key}/responses`.
struct ReadingEngine: Sendable {
    static let sourceName = "saved-page"
    static let blockOutFields = [
        "key", "position", "type", "is_gate", "content", "state", "attempts", "revealed", "segments", "concepts"
    ]
    static let confidences = ["low", "medium", "high"]

    var bundle: OfflineBundle

    struct BlockState: Sendable {
        var block: JSONValue
        var key: String
        var view: JSONValue
        var state: String?
        var visible: Bool
        var cleared: Bool
        var passed: Bool
        var isGate: Bool
    }

    func states() -> [BlockState] {
        var out: [BlockState] = []
        var visible = true
        for block in bundle.blocks {
            let responses = block["responses"].array ?? []
            let attempts = responses.count
            let passed = responses.contains { $0["passed"].bool == true }
            let isGate = block["is_gate"].bool == true
            let state: String? = !isGate ? nil : passed ? "passed" : attempts >= bundle.maxAttempts ? "revealed" : "open"
            let cleared = state == "passed" || state == "revealed"
            var view: [String: JSONValue] = [:]
            for field in Self.blockOutFields { view[field] = block[field] }
            view["state"] = state.map(JSONValue.string) ?? .null
            view["attempts"] = .int(attempts)
            view["revealed"] = cleared ? block["private"] : .null
            out.append(BlockState(
                block: block, key: block["key"].string ?? "", view: .object(view), state: state,
                visible: visible, cleared: cleared, passed: passed, isGate: isGate
            ))
            if state == "open" { visible = false }
        }
        return out
    }

    /// `GET /api/artifacts/{slug}/blocks`.
    func blocksView() -> JSONValue {
        let states = states()
        return .object([
            "artifact": .string(bundle.slug),
            "total_blocks": .int(states.count),
            "locked_blocks": .int(states.filter { !$0.visible }.count),
            "complete": .bool(states.filter(\.isGate).allSatisfy(\.cleared)),
            "blocks": .array(states.filter(\.visible).map(\.view))
        ])
    }

    /// `GET /api/artifacts/{slug}/blocks/{key}/responses`.
    func responsesView(key: String) throws(EngineError) -> JSONValue {
        let state = try visibleState(key)
        let rows = (state.block["responses"].array ?? []).map { row -> JSONValue in
            guard case .object(var fields) = row else { return row }
            if !state.cleared { fields["details"] = .null }
            return .object(fields)
        }
        return .array(rows)
    }

    private func visibleState(_ key: String) throws(EngineError) -> BlockState {
        guard let state = states().first(where: { $0.key == key }) else {
            throw EngineError(status: 404, detail: "\(bundle.slug): no block \(Python.repr(key))")
        }
        guard state.visible else {
            throw EngineError(status: 409, detail: "block \(key) is locked: clear the gate before it first")
        }
        return state
    }

    // MARK: Submitting

    /// A checked submission, ready to be graded.
    struct Submission: Sendable {
        var key: String
        var type: String
        var block: JSONValue
        var response: JSONValue
        var confidence: String?
        /// For a blurb: the validated answer text.
        var answerText: String?
    }

    /// Check a `POST .../responses` body in the server's order (`submit_response` and its
    /// grader's validation); nothing is graded or recorded yet. `onlyOpenGates`: refuse an
    /// attempt at a "revealed" gate (made offline it would be rejected on upload).
    func prepare(key: String, body: JSONValue, onlyOpenGates: Bool) throws(EngineError) -> Submission {
        guard case .object(let fields) = body else {
            throw EngineError(status: 422, detail: "body: Input should be a valid dictionary or object to extract fields from")
        }
        if let unknown = fields.keys.sorted().first(where: { $0 != "response" && $0 != "confidence" }) {
            throw EngineError(status: 422, detail: "\(unknown): Extra inputs are not permitted")
        }
        guard let response = fields["response"] else { throw EngineError(status: 422, detail: "response: Field required") }
        guard case .object = response else {
            throw EngineError(status: 422, detail: "response: Input should be a valid dictionary")
        }
        var confidence: String?
        switch fields["confidence"] ?? .null {
        case .null: confidence = nil
        case .string(let value) where Self.confidences.contains(value): confidence = value
        default: throw EngineError(status: 422, detail: "confidence: Input should be 'low', 'medium' or 'high'")
        }

        guard let state = states().first(where: { $0.key == key }) else {
            throw EngineError(status: 404, detail: "\(bundle.slug): no block \(Python.repr(key))")
        }
        guard state.visible else {
            throw EngineError(status: 409, detail: "block \(key) is locked: clear the gate before it first")
        }
        guard state.isGate else { throw EngineError(status: 409, detail: "block \(key) is not a gate and takes no responses") }
        guard !state.passed else { throw EngineError(status: 409, detail: "block \(key) is already passed") }
        if onlyOpenGates, state.state == "revealed" {
            throw EngineError(status: 409, detail: "This checkpoint has used all its attempts and its answer is shown. "
                + "An attempt made offline would not count, so the phone does not take one; answer it when the server can be reached.")
        }
        let type = state.block["type"].string ?? ""
        guard ["multiple_choice", "prediction", "blurb"].contains(type) else {
            throw EngineError(status: 422, detail: "no grader for block type \(Python.repr(type))")
        }
        if confidence != nil, state.block["is_question"].bool != true {
            throw EngineError(status: 422, detail: "block \(key) is a \(type), not a question, and takes no confidence")
        }
        var submission = Submission(key: key, type: type, block: state.block, response: response, confidence: confidence)
        if type == "blurb" {
            submission.answerText = try answerText(state.block, response)
        } else {
            _ = try choice(state.block, response)
        }
        return submission
    }

    /// `teacher.grading._require_keys`.
    private func requireKeys(_ response: JSONValue, _ expected: Set<String>) throws(EngineError) -> [String: JSONValue] {
        guard case .object(let fields) = response else {
            throw EngineError(status: 422, detail: "response must be an object, got \(response.pythonTypeName)")
        }
        let missing = expected.subtracting(fields.keys)
        if !missing.isEmpty { throw EngineError(status: 422, detail: "response is missing \(missing.sorted().joined(separator: ", "))") }
        let unknown = Set(fields.keys).subtracting(expected)
        if !unknown.isEmpty {
            throw EngineError(status: 422, detail: "response has unknown key(s) \(unknown.sorted().joined(separator: ", "))")
        }
        return fields
    }

    /// `teacher.grading._choice`.
    private func choice(_ block: JSONValue, _ response: JSONValue) throws(EngineError) -> Int {
        let value = try requireKeys(response, ["choice"])["choice"] ?? .null
        guard case .int(let choice) = value else {
            throw EngineError(status: 422, detail: "choice must be an integer option index")
        }
        let count = block["content"]["options"].array?.count ?? 0
        guard choice >= 0, choice < count else {
            throw EngineError(status: 422, detail: "choice must be between 0 and \(count - 1), got \(choice)")
        }
        return choice
    }

    /// `teacher.judge._answer_text`.
    private func answerText(_ block: JSONValue, _ response: JSONValue) throws(EngineError) -> String {
        let value = try requireKeys(response, ["text"])["text"] ?? .null
        guard case .string(let text) = value else { throw EngineError(status: 422, detail: "text must be a string") }
        guard !Python.isBlank(text) else { throw EngineError(status: 422, detail: "text must not be empty") }
        let minWords = block["content"]["min_words"].int ?? bundle.defaultMinWords
        let words = Python.words(text).count
        if words < minWords {
            throw EngineError(status: 422, detail: "Write at least \(minWords) words before submitting (you wrote \(words)); "
                + "this was not counted as an attempt.")
        }
        if (block["private"]["rubric"].array ?? []).isEmpty {
            throw EngineError(status: 422, detail: "blurb rubric is empty; there is nothing to grade against")
        }
        return text
    }

    /// The deterministic grade of a multiple choice or a prediction (nil for a blurb, which
    /// the device grader grades).
    func deterministicGrade(_ submission: Submission) -> Grade? {
        guard submission.type != "blurb", let choice = submission.response["choice"].int else { return nil }
        let grading = bundle.grading
        let privatePart = submission.block["private"]
        let correct = privatePart["correct_index"].int
        if submission.type == "prediction" {
            return Grade(
                verdict: "passed", feedback: grading["prediction_feedback"].string, missing: [],
                details: .object(["correct": .bool(choice == correct)]), grader: grading["prediction_grader"].string ?? ""
            )
        }
        let grader = grading["multiple_choice_grader"].string ?? ""
        if choice == correct {
            return Grade(verdict: "passed", feedback: privatePart["explanation"].string, missing: [], details: .null, grader: grader)
        }
        guard let explanations = privatePart["option_explanations"].array else {
            return Grade(verdict: "failed", feedback: grading["no_option_feedback"].string, missing: [], details: .null, grader: grader)
        }
        let prefix = grading["multiple_choice_wrong_prefix"].string ?? ""
        let explanation = choice < explanations.count ? explanations[choice].string ?? "" : ""
        return Grade(verdict: "failed", feedback: prefix + explanation, missing: [], details: .null, grader: grader)
    }

    /// The recorded attempt, the reply the server would give, and the engine after it.
    struct Recorded: Sendable {
        var row: JSONValue
        var reply: JSONValue
        var engine: ReadingEngine
    }

    /// Record a graded submission made at `madeAt` (`POST .../responses`).
    func record(_ submission: Submission, grade: Grade, madeAt: Instant) -> Recorded {
        let attempts = (submission.block["responses"].array ?? []).count
        let row = JSONValue.object([
            "attempt": .int(attempts + 1),
            "response": submission.response,
            "passed": .bool(grade.verdict == "passed"),
            "verdict": .string(grade.verdict),
            "feedback": grade.feedback.map(JSONValue.string) ?? .null,
            "missing": .array(grade.missing.map(JSONValue.string)),
            "confidence": submission.confidence.map(JSONValue.string) ?? .null,
            "grader": .string(grade.grader),
            "details": grade.details,
            "created_at": .string(madeAt.iso)
        ])
        let after = ReadingEngine(bundle: bundle.adding([(submission.key, row)]))
        return Recorded(row: row, reply: Self.reply(before: self, after: after, key: submission.key, recorded: row), engine: after)
    }

    /// The reply to a submission the server accepted, computed from the bundle before and
    /// the server's bundle after it (`recorded` is the stored attempt as the upload returned it).
    static func reply(before: ReadingEngine, after: ReadingEngine, key: String, recorded: JSONValue) -> JSONValue {
        let wasVisible = Set(before.states().filter(\.visible).map(\.key))
        let states = after.states()
        let now = states.first { $0.key == key }
        return .object([
            "attempt": recorded["attempt"],
            "passed": recorded["passed"],
            "verdict": recorded["verdict"],
            "feedback": recorded["feedback"],
            "missing": recorded["missing"],
            "confidence": recorded["confidence"],
            "details": now?.cleared == true ? recorded["details"] : .null,
            "state": now?.state.map(JSONValue.string) ?? .null,
            "revealed": now?.view["revealed"] ?? .null,
            "unlocked": .array(states.filter { $0.visible && !wasVisible.contains($0.key) }.map(\.view)),
            "locked_blocks": .int(states.filter { !$0.visible }.count),
            "complete": .bool(states.filter(\.isGate).allSatisfy(\.cleared))
        ])
    }

    // MARK: Progress

    var gatesTotal: Int { states().filter(\.isGate).count }
    var gatesCleared: Int { states().filter { $0.isGate && $0.cleared }.count }
}

import Foundation

/// A highlight change made on the phone and not yet uploaded (an item of
/// `POST /api/offline/artifacts/{slug}/highlights`).
struct HighlightChange: Codable, Sendable, Equatable, Identifiable {
    var changeID: UUID
    var highlightID: String
    var op: String // swiftlint:disable:this identifier_name
    var madeAt: Instant
    /// The change as uploaded (`{"op", "id", ..., "made_at"}`).
    var upload: JSONValue
    /// The passage, for the sync log.
    var quote: String

    var id: UUID { changeID }
}

/// A saved page's highlights and notes: the list as the phone shows it, ids it knows were
/// removed (a removed id stays taken, as on the server) and the changes waiting to upload.
/// `list` is nil when the page was saved from a server whose bundle has no highlights.
struct SavedHighlights: Codable, Sendable, Equatable {
    var list: [JSONValue]?
    var removed: [String] = []
    var changes: [HighlightChange] = []
}

/// The highlight endpoints of a saved page, answered on the phone exactly as
/// `teacher.web.routers.highlights` says ("Reproducing these endpoints offline") and as
/// `teacher.managers.highlights` checks: the same JSON, the same statuses and messages.
struct HighlightEngine: Sendable {
    static let maxQuote = 4000
    static let maxContext = 64
    static let maxNote = 10000
    static let anchorFields = ["id", "block_key", "quote", "prefix", "suffix", "start", "end", "note"]

    var slug: String
    /// The page's blocks (each with `key` and `position`).
    var blocks: [JSONValue]
    var saved: SavedHighlights

    enum Route: Equatable {
        case list(slug: String)
        case create(slug: String)
        case update(slug: String, id: String)
        case delete(slug: String, id: String)

        var slug: String {
            switch self {
            case .list(let slug), .create(let slug), .update(let slug, _), .delete(let slug, _): slug
            }
        }
    }

    static func route(_ request: GatewayRequest) -> Route? {
        let parts = request.path.split(separator: "/", omittingEmptySubsequences: false).map {
            String($0).removingPercentEncoding ?? String($0)
        }
        // "", "api", "artifacts", slug, "highlights", [id]
        guard parts.count >= 5, parts.count <= 6, parts[0].isEmpty, parts[1] == "api", parts[2] == "artifacts", !parts[3].isEmpty,
              parts[4] == "highlights" else { return nil }
        if parts.count == 5 {
            switch request.method {
            case "GET": return .list(slug: parts[3])
            case "POST": return .create(slug: parts[3])
            default: return nil
            }
        }
        switch request.method {
        case "PATCH": return .update(slug: parts[3], id: parts[5])
        case "DELETE": return .delete(slug: parts[3], id: parts[5])
        default: return nil
        }
    }

    // MARK: Reading

    static let notSaved = EngineError(status: 503, detail: "This page was saved without its highlights (the server did not "
        + "send them), so they cannot be shown or changed offline. Refresh the saved copy when the server can be reached.")

    private func requireList() throws(EngineError) {
        if saved.list == nil { throw Self.notSaved }
    }

    func position(of key: String) -> Int? {
        blocks.first { $0["key"].string == key }?["position"].int
    }

    /// `GET .../highlights`: in page order (by block position, unknown blocks last, then by
    /// start, then oldest first).
    func listView() throws(EngineError) -> JSONValue {
        guard let list = saved.list else { throw Self.notSaved }
        let ordered = list.enumerated().sorted { lhs, rhs in
            let left = (lhs.element["block_position"].int == nil ? 1 : 0, lhs.element["block_position"].int ?? 0,
                        lhs.element["start"].int ?? 0)
            let right = (rhs.element["block_position"].int == nil ? 1 : 0, rhs.element["block_position"].int ?? 0,
                         rhs.element["start"].int ?? 0)
            if left != right { return left < right }
            let times = (lhs.element["created_at"].string ?? "", rhs.element["created_at"].string ?? "")
            if times.0 != times.1 { return times.0 < times.1 }
            return lhs.offset < rhs.offset
        }.map(\.element)
        return .object(["artifact": .string(slug), "highlights": .array(ordered)])
    }

    // MARK: Changing

    /// `POST .../highlights`: the highlight made (201) and the change to upload.
    mutating func create(_ body: JSONValue?, now: Instant, newID: () -> UUID = { UUID() }) throws(EngineError)
        -> (highlight: JSONValue, change: HighlightChange) {
        try requireList()
        let fields = try Self.validateCreate(body)
        let anchor = try Self.checkAnchor(fields)
        let note = try Self.cleanNote(fields["note"]?.string)
        guard let position = position(of: anchor.blockKey) else {
            throw EngineError(status: 422, detail: "the page \(Python.repr(slug)) has no block \(Python.repr(anchor.blockKey))")
        }
        var id = newID().uuidString.lowercased()
        if let given = fields["id"], !given.isNull { id = try Self.uuid(given, field: "id") }
        if saved.list?.contains(where: { $0["id"].string == id }) == true || saved.removed.contains(id) {
            throw EngineError(status: 409, detail: "a highlight with id \(id) exists already")
        }
        let highlight = JSONValue.object([
            "id": .string(id), "artifact": .string(slug), "block_key": .string(anchor.blockKey), "block_position": .int(position),
            "quote": .string(anchor.quote), "prefix": .string(anchor.prefix), "suffix": .string(anchor.suffix),
            "start": .int(anchor.start), "end": .int(anchor.end), "note": note.map(JSONValue.string) ?? .null,
            "created_at": .string(now.iso), "updated_at": .string(now.iso)
        ])
        saved.list?.append(highlight)
        let change = HighlightChange(changeID: UUID(), highlightID: id, op: "create", madeAt: now, upload: .object([
            "op": .string("create"), "id": .string(id), "block_key": .string(anchor.blockKey), "quote": .string(anchor.quote),
            "prefix": .string(anchor.prefix), "suffix": .string(anchor.suffix), "start": .int(anchor.start), "end": .int(anchor.end),
            "note": note.map(JSONValue.string) ?? .null, "made_at": .string(now.iso)
        ]), quote: anchor.quote)
        saved.changes.append(change)
        return (highlight, change)
    }

    /// `PATCH .../highlights/{id}`: the highlight with its new note (200) and the change.
    mutating func updateNote(id raw: String, _ body: JSONValue?, now: Instant) throws(EngineError) -> JSONValue {
        try requireList()
        var errors: [String] = []
        let id: String?
        do { id = try Self.uuid(.string(raw), field: "highlight_id") } catch { id = nil; errors.append(error.detail) }
        var note: JSONValue = .null
        do { note = try Self.validateNote(body) } catch { errors.append(error.detail) }
        guard errors.isEmpty, let id else { throw EngineError(status: 422, detail: errors.joined(separator: "; ")) }
        guard let index = saved.list?.firstIndex(where: { $0["id"].string == id }), case .object(var fields)? = saved.list?[index] else {
            throw EngineError(status: 404, detail: "no highlight \(id) on \(Python.repr(slug))")
        }
        let cleaned = try Self.cleanNote(note.string)
        fields["note"] = cleaned.map(JSONValue.string) ?? .null
        fields["updated_at"] = .string(now.iso)
        saved.list?[index] = .object(fields)
        saved.changes.append(HighlightChange(changeID: UUID(), highlightID: id, op: "update_note", madeAt: now, upload: .object([
            "op": .string("update_note"), "id": .string(id), "note": cleaned.map(JSONValue.string) ?? .null, "made_at": .string(now.iso)
        ]), quote: fields["quote"]?.string ?? ""))
        return .object(fields)
    }

    /// `DELETE .../highlights/{id}` (204). A highlight made on the phone and not uploaded yet
    /// is simply forgotten with its changes: the server never needs to hear of it.
    mutating func delete(id raw: String, now: Instant) throws(EngineError) {
        try requireList()
        let id = try Self.uuid(.string(raw), field: "highlight_id")
        guard let index = saved.list?.firstIndex(where: { $0["id"].string == id }) else {
            throw EngineError(status: 404, detail: "no highlight \(id) on \(Python.repr(slug))")
        }
        let quote = saved.list?[index]["quote"].string ?? ""
        saved.list?.remove(at: index)
        saved.removed.append(id)
        if saved.changes.contains(where: { $0.highlightID == id && $0.op == "create" }) {
            saved.changes.removeAll { $0.highlightID == id }
        } else {
            saved.changes.append(HighlightChange(changeID: UUID(), highlightID: id, op: "delete", madeAt: now, upload: .object([
                "op": .string("delete"), "id": .string(id), "made_at": .string(now.iso)
            ]), quote: quote))
        }
    }

    /// The list after an upload: the server's, with the changes still waiting applied on top.
    static func replay(_ changes: [HighlightChange], onto list: [JSONValue], slug: String, blocks: [JSONValue]) -> [JSONValue] {
        var out = list
        let engine = HighlightEngine(slug: slug, blocks: blocks, saved: SavedHighlights())
        for change in changes {
            let upload = change.upload
            let index = out.firstIndex { $0["id"].string == change.highlightID }
            switch change.op {
            case "create" where index == nil:
                var fields: [String: JSONValue] = [:]
                for key in ["id", "block_key", "quote", "prefix", "suffix", "start", "end", "note"] { fields[key] = upload[key] }
                fields["artifact"] = .string(slug)
                fields["block_position"] = engine.position(of: upload["block_key"].string ?? "").map(JSONValue.int) ?? .null
                fields["created_at"] = upload["made_at"]
                fields["updated_at"] = upload["made_at"]
                out.append(.object(fields))
            case "update_note":
                guard let index, case .object(var fields) = out[index] else { continue }
                fields["note"] = upload["note"]
                fields["updated_at"] = upload["made_at"]
                out[index] = .object(fields)
            case "delete":
                if let index { out.remove(at: index) }
            default:
                continue
            }
        }
        return out
    }

    // MARK: The server's checks

    struct Anchor {
        var blockKey: String
        var quote: String
        var prefix: String
        var suffix: String
        var start: Int
        var end: Int
    }

    static let notAnObject = "body: Input should be a valid dictionary or object to extract fields from"

    /// `HighlightIn` (pydantic, extra keys forbidden): every field error, in field order, then
    /// the unknown keys, joined as the server's validation handler joins them.
    static func validateCreate(_ body: JSONValue?) throws(EngineError) -> [String: JSONValue] {
        guard case .object(let fields)? = body else { throw EngineError(status: 422, detail: notAnObject) }
        var errors: [String] = []
        if let id = fields["id"], !id.isNull {
            do { _ = try uuid(id, field: "id") } catch { errors.append(error.detail) }
        }
        for name in ["block_key", "quote"] {
            switch fields[name] {
            case nil: errors.append("\(name): Field required")
            case .string?: break
            default: errors.append("\(name): Input should be a valid string")
            }
        }
        for name in ["prefix", "suffix"] {
            if let value = fields[name], value.string == nil { errors.append("\(name): Input should be a valid string") }
        }
        for name in ["start", "end"] {
            switch fields[name] {
            case nil: errors.append("\(name): Field required")
            case .int?: break
            default: errors.append("\(name): Input should be a valid integer")
            }
        }
        if let note = fields["note"], !note.isNull, note.string == nil { errors.append("note: Input should be a valid string") }
        for name in fields.keys.sorted() where !anchorFields.contains(name) {
            errors.append("\(name): Extra inputs are not permitted")
        }
        if !errors.isEmpty { throw EngineError(status: 422, detail: errors.joined(separator: "; ")) }
        return fields
    }

    /// `NoteIn`: `{"note": <string> | null}`, nothing else.
    static func validateNote(_ body: JSONValue?) throws(EngineError) -> JSONValue {
        guard case .object(let fields)? = body else { throw EngineError(status: 422, detail: notAnObject) }
        var errors: [String] = []
        switch fields["note"] {
        case nil: errors.append("note: Field required")
        case .null?, .string?: break
        default: errors.append("note: Input should be a valid string")
        }
        for name in fields.keys.sorted() where name != "note" { errors.append("\(name): Extra inputs are not permitted") }
        if !errors.isEmpty { throw EngineError(status: 422, detail: errors.joined(separator: "; ")) }
        return fields["note"] ?? .null
    }

    /// `teacher.managers.highlights.check_anchor`.
    static func checkAnchor(_ fields: [String: JSONValue]) throws(EngineError) -> Anchor {
        let anchor = Anchor(
            blockKey: fields["block_key"]?.string ?? "", quote: fields["quote"]?.string ?? "",
            prefix: fields["prefix"]?.string ?? "", suffix: fields["suffix"]?.string ?? "",
            start: fields["start"]?.int ?? 0, end: fields["end"]?.int ?? 0
        )
        func fail(_ detail: String) -> EngineError { EngineError(status: 422, detail: detail) }
        if Python.isBlank(anchor.blockKey) { throw fail("block_key must be a block's key") }
        if Python.isBlank(anchor.quote) { throw fail("quote must be the highlighted text, not blank") }
        if anchor.blockKey.contains("\u{0}") { throw fail("block_key contains a NUL character") }
        if anchor.quote.contains("\u{0}") { throw fail("quote contains a NUL character") }
        let quoteLength = anchor.quote.unicodeScalars.count
        if quoteLength > maxQuote { throw fail("quote is \(quoteLength) characters long; at most \(maxQuote) are kept") }
        for (name, value) in [("prefix", anchor.prefix), ("suffix", anchor.suffix)] {
            if value.contains("\u{0}") { throw fail("\(name) contains a NUL character") }
            let length = value.unicodeScalars.count
            if length > maxContext { throw fail("\(name) is \(length) characters long; at most \(maxContext) are kept") }
        }
        guard anchor.start >= 0, anchor.start < anchor.end else {
            throw fail("0 <= start < end is required, got start \(anchor.start), end \(anchor.end)")
        }
        return anchor
    }

    /// `clean_note`: none for none or a blank note; at most 10000 characters once stripped.
    static func cleanNote(_ note: String?) throws(EngineError) -> String? {
        guard let note else { return nil }
        if note.contains("\u{0}") { throw EngineError(status: 422, detail: "note contains a NUL character") }
        let text = Python.strip(note)
        let length = text.unicodeScalars.count
        if length > maxNote { throw EngineError(status: 422, detail: "note is \(length) characters long; the limit is \(maxNote)") }
        return text.isEmpty ? nil : text
    }

    /// A UUID in canonical lowercase form, from the forms pydantic takes (hyphenated or 32 hex
    /// digits, optionally in braces or after `urn:uuid:`); else pydantic's message.
    static func uuid(_ value: JSONValue, field: String) throws(EngineError) -> String {
        guard case .string(let text) = value else {
            throw EngineError(status: 422, detail: "\(field): UUID input should be a string, bytes or UUID object")
        }
        var body = Substring(text)
        if body.lowercased().hasPrefix("urn:uuid:") { body = body.dropFirst(9) }
        if body.hasPrefix("{"), body.hasSuffix("}") { body = body.dropFirst().dropLast() }
        let hex = body.filter { $0 != "-" }
        if let bad = body.enumerated().first(where: { !$0.element.isHexDigit && $0.element != "-" }) {
            throw EngineError(status: 422,
                              detail: "\(field): Input should be a valid UUID, invalid character: found `\(bad.element)` at \(bad.offset)")
        }
        let groups = body.split(separator: "-", omittingEmptySubsequences: false).map(\.count)
        guard hex.count == 32, groups == [32] || groups == [8, 4, 4, 4, 12],
              let uuid = UUID(uuidString: Self.hyphenated(String(hex))) else {
            throw EngineError(status: 422, detail: "\(field): Input should be a valid UUID, invalid length: expected 32 "
                + "hexadecimal digits, found \(hex.count)")
        }
        return uuid.uuidString.lowercased()
    }

    private static func hyphenated(_ hex: String) -> String {
        let chars = Array(hex)
        guard chars.count == 32 else { return hex }
        return [0..<8, 8..<12, 12..<16, 16..<20, 20..<32].map { String(chars[$0]) }.joined(separator: "-")
    }
}

extension Python {
    /// `str.strip()` with no argument.
    static func strip(_ text: String) -> String {
        let scalars = Array(text.unicodeScalars)
        guard let first = scalars.firstIndex(where: { !isSpace($0) }), let last = scalars.lastIndex(where: { !isSpace($0) }) else {
            return ""
        }
        var out = String.UnicodeScalarView()
        out.append(contentsOf: scalars[first...last])
        return String(out)
    }
}

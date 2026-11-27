import Foundation

/// Any JSON value, kept exactly: integers stay integers, booleans stay booleans, so what the
/// phone computes compares equal to what the server sends.
enum JSONValue: Sendable, Equatable, Hashable {
    case null
    case bool(Bool)
    case int(Int)
    case double(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    // MARK: Reading

    struct ParseError: Error, LocalizedError {
        var message: String
        var errorDescription: String? { message }
    }

    init(data: Data) throws {
        let object: Any
        do {
            object = try JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
        } catch {
            throw ParseError(message: "not JSON (\(error.localizedDescription))")
        }
        self = try JSONValue(foundation: object)
    }

    init(foundation object: Any) throws {
        switch object {
        case is NSNull:
            self = .null
        case let number as NSNumber:
            if CFGetTypeID(number) == CFBooleanGetTypeID() {
                self = .bool(number.boolValue)
            } else if CFNumberIsFloatType(number) {
                self = .double(number.doubleValue)
            } else {
                self = .int(number.intValue)
            }
        case let string as String:
            self = .string(string)
        case let array as [Any]:
            self = .array(try array.map(JSONValue.init(foundation:)))
        case let dictionary as [String: Any]:
            self = .object(try dictionary.mapValues(JSONValue.init(foundation:)))
        default:
            throw ParseError(message: "unexpected JSON value \(type(of: object))")
        }
    }

    // MARK: Writing

    var foundation: Any {
        switch self {
        case .null: NSNull()
        case .bool(let value): NSNumber(value: value)
        case .int(let value): NSNumber(value: value)
        case .double(let value): NSNumber(value: value)
        case .string(let value): value
        case .array(let values): values.map(\.foundation)
        case .object(let values): values.mapValues(\.foundation)
        }
    }

    func data(sorted: Bool = false) -> Data {
        var options: JSONSerialization.WritingOptions = [.fragmentsAllowed, .withoutEscapingSlashes]
        if sorted { options.insert(.sortedKeys) }
        // Every value here came from JSON or from the engine, so it always serializes.
        return (try? JSONSerialization.data(withJSONObject: foundation, options: options)) ?? Data("null".utf8)
    }

    // MARK: Access

    subscript(key: String) -> JSONValue {
        if case .object(let values) = self { return values[key] ?? .null }
        return .null
    }

    var isNull: Bool { self == .null }
    var string: String? { if case .string(let value) = self { value } else { nil } }
    var int: Int? { if case .int(let value) = self { value } else { nil } }
    var bool: Bool? { if case .bool(let value) = self { value } else { nil } }
    var array: [JSONValue]? { if case .array(let values) = self { values } else { nil } }
    var object: [String: JSONValue]? { if case .object(let values) = self { values } else { nil } }
    /// A number, whether JSON wrote it as an integer or not (`3.0` may arrive as either).
    var number: Double? {
        switch self {
        case .int(let value): Double(value)
        case .double(let value): value
        default: nil
        }
    }

    /// The name Python gives this value's type (`int`, `str`, ...), as in the server's messages.
    var pythonTypeName: String {
        switch self {
        case .null: "NoneType"
        case .bool: "bool"
        case .int: "int"
        case .double: "float"
        case .string: "str"
        case .array: "list"
        case .object: "dict"
        }
    }
}

extension JSONValue: Codable {
    init(from decoder: any Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Int.self) {
            self = .int(value)
        } else if let value = try? container.decode(Double.self) {
            self = .double(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            self = .object(try container.decode([String: JSONValue].self))
        }
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null: try container.encodeNil()
        case .bool(let value): try container.encode(value)
        case .int(let value): try container.encode(value)
        case .double(let value): try container.encode(value)
        case .string(let value): try container.encode(value)
        case .array(let values): try container.encode(values)
        case .object(let values): try container.encode(values)
        }
    }
}

/// Python's conventions where the server's output depends on them.
enum Python {
    /// `str.split()` with no argument: runs of whitespace (`str.isspace`) separate words.
    static func words(_ text: String) -> [Substring] {
        text.split { char in char.unicodeScalars.allSatisfy(isSpace) }
    }

    /// `str.strip()` is empty.
    static func isBlank(_ text: String) -> Bool {
        text.unicodeScalars.allSatisfy(isSpace)
    }

    /// `str.isspace` for one character: Unicode White_Space plus the four separators
    /// U+001C...U+001F, which Python also counts.
    static func isSpace(_ scalar: Unicode.Scalar) -> Bool {
        scalar.properties.isWhitespace || (0x1C...0x1F).contains(scalar.value)
    }

    /// `repr()` of a string, as in the server's error messages (`'b09'`).
    static func repr(_ text: String) -> String {
        let quote: Character = text.contains("'") && !text.contains("\"") ? "\"" : "'"
        var out = String(quote)
        for char in text {
            switch char {
            case "\\": out += "\\\\"
            case "\n": out += "\\n"
            case "\r": out += "\\r"
            case "\t": out += "\\t"
            case quote: out += "\\\(quote)"
            default: out.append(char)
            }
        }
        out.append(quote)
        return out
    }
}

/// A moment with microsecond precision, written as the server writes times (`to_iso`):
/// `2026-11-25T10:00:00+00:00`, with `.ffffff` only when the microseconds are not zero.
struct Instant: Sendable, Equatable, Hashable, Codable, Comparable {
    /// Microseconds since 1970 (UTC).
    var micros: Int64

    init(micros: Int64) {
        self.micros = micros
    }

    init(_ date: Date) {
        micros = Int64((date.timeIntervalSince1970 * 1_000_000).rounded(.down))
    }

    static func now() -> Instant { Instant(Date()) }

    var date: Date { Date(timeIntervalSince1970: Double(micros) / 1_000_000) }

    static func < (lhs: Instant, rhs: Instant) -> Bool { lhs.micros < rhs.micros }

    /// A server time (`2026-11-25T10:00:00[.ffffff]+00:00`; any `±HH:MM` offset or `Z`); nil for
    /// anything else.
    init?(isoString text: String) {
        let pattern = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/
        guard let match = text.wholeMatch(of: pattern) else { return nil }
        var parts = DateComponents()
        parts.year = Int(match.output.1)
        parts.month = Int(match.output.2)
        parts.day = Int(match.output.3)
        parts.hour = Int(match.output.4)
        parts.minute = Int(match.output.5)
        parts.second = Int(match.output.6)
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC") ?? .gmt
        guard let date = calendar.date(from: parts) else { return nil }
        let fraction = match.output.7.map { Int64(String($0).padding(toLength: 6, withPad: "0", startingAt: 0)) ?? 0 } ?? 0
        var offsetSeconds: Int64 = 0
        let zone = String(match.output.8)
        if zone != "Z" {
            let sign: Int64 = zone.hasPrefix("-") ? -1 : 1
            let hours = Int64(zone.dropFirst().prefix(2)) ?? 0
            let minutes = Int64(zone.suffix(2)) ?? 0
            offsetSeconds = sign * (hours * 3600 + minutes * 60)
        }
        self.init(micros: (Int64(date.timeIntervalSince1970) - offsetSeconds) * 1_000_000 + fraction)
    }

    /// This moment plus `days` (fractional) days, to the microsecond.
    func adding(days: Double) -> Instant {
        Instant(micros: micros + Int64((days * 86_400 * 1_000_000).rounded()))
    }

    /// Python's `isoformat()` of this moment in UTC.
    var iso: String {
        let seconds = micros >= 0 ? micros / 1_000_000 : (micros - 999_999) / 1_000_000
        let fraction = micros - seconds * 1_000_000
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC") ?? .gmt
        let parts = calendar.dateComponents(
            [.year, .month, .day, .hour, .minute, .second], from: Date(timeIntervalSince1970: Double(seconds))
        )
        var text = String(
            format: "%04d-%02d-%02dT%02d:%02d:%02d",
            parts.year ?? 0, parts.month ?? 0, parts.day ?? 0, parts.hour ?? 0, parts.minute ?? 0, parts.second ?? 0
        )
        if fraction != 0 { text += String(format: ".%06lld", fraction) }
        return text + "+00:00"
    }
}

extension JSONValue: ExpressibleByStringLiteral, ExpressibleByIntegerLiteral, ExpressibleByBooleanLiteral {
    init(stringLiteral value: String) { self = .string(value) }
    init(integerLiteral value: Int) { self = .int(value) }
    init(booleanLiteral value: Bool) { self = .bool(value) }
}

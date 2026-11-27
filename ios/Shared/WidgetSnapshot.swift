import Foundation

/// A calendar day ("2026-11-26") in some time zone, without the zone: the unit of the server's
/// `daily_answers` and of the daily goal. Arithmetic on days is done on a UTC calendar, where
/// every day has 24 hours; the zone is only used to put a moment on its day.
struct LocalDay: Hashable, Comparable, Codable, Sendable, CustomStringConvertible {
    var year: Int
    var month: Int
    var day: Int

    init(year: Int, month: Int, day: Int) {
        (self.year, self.month, self.day) = (year, month, day)
    }

    /// "YYYY-MM-DD" and nothing else (nil for anything else, or a date that does not exist).
    init?(_ text: String) {
        guard let match = text.wholeMatch(of: /(\d{4})-(\d{2})-(\d{2})/),
              let year = Int(match.output.1), let month = Int(match.output.2), let day = Int(match.output.3) else { return nil }
        let candidate = LocalDay(year: year, month: month, day: day)
        guard let date = candidate.utcNoon, LocalDay(date, in: Self.utc) == candidate else { return nil }
        self = candidate
    }

    /// The day `date` falls on in `zone`.
    init(_ date: Date, in zone: TimeZone) {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        self.init(year: parts.year ?? 1970, month: parts.month ?? 1, day: parts.day ?? 1)
    }

    var description: String { String(format: "%04d-%02d-%02d", year, month, day) }

    static func < (lhs: LocalDay, rhs: LocalDay) -> Bool { (lhs.year, lhs.month, lhs.day) < (rhs.year, rhs.month, rhs.day) }

    private static let utc = TimeZone(identifier: "UTC") ?? .gmt

    private var utcNoon: Date? {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = Self.utc
        return calendar.date(from: DateComponents(year: year, month: month, day: day, hour: 12))
    }

    func adding(days: Int) -> LocalDay {
        guard let noon = utcNoon else { return self }
        return LocalDay(noon.addingTimeInterval(Double(days) * 86_400), in: Self.utc)
    }

    /// Days from `self` to `other` (positive when `other` is later).
    func days(to other: LocalDay) -> Int {
        guard let start = utcNoon, let end = other.utcNoon else { return 0 }
        return Int((end.timeIntervalSince(start) / 86_400).rounded())
    }

    /// The first moment of the day in `zone` (midnight, or the first hour that exists).
    func start(in zone: TimeZone) -> Date {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let noon = calendar.date(from: DateComponents(year: year, month: month, day: day, hour: 12)) ?? Date(timeIntervalSince1970: 0)
        return calendar.startOfDay(for: noon)
    }

    /// Monday 1 ... Sunday 7.
    var isoWeekday: Int {
        guard let noon = utcNoon else { return 1 }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = Self.utc
        let weekday = calendar.component(.weekday, from: noon) // Sunday 1
        return weekday == 1 ? 7 : weekday - 1
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.singleValueContainer()
        let text = try container.decode(String.self)
        guard let day = LocalDay(text) else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "not a YYYY-MM-DD day: \(text)")
        }
        self = day
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(description)
    }
}

/// What the app hands its widget (and the Shortcuts that read without opening the app): a small
/// file in the App Group container (`SharedContainer`), written by the app whenever what it
/// knows changes. It holds no session token and nothing the widget could use to reach the server.
///
/// The review numbers hold at `computedAt`; `upcoming` lists the moments after it at which one
/// more question comes due, so the widget can raise its count on time without the app running
/// (`ReviewProjection`).
struct WidgetSnapshot: Codable, Equatable, Sendable {
    static let formatVersion = 1
    /// After this long the widget says how old its numbers are ("Updated 14:05"). The due times
    /// are projected exactly, so a count stays right as time passes; what the snapshot cannot
    /// know is what happened elsewhere since (reviews answered on the web, new questions from a
    /// page studied on the computer). The app refreshes it whenever it runs and, when iOS lets
    /// it, in the background roughly hourly; three hours without a refresh means the app has not
    /// run for a while, which is when the reader should be told.
    static let staleAfter: TimeInterval = 3 * 3600

    var version: Int = formatVersion
    var writtenAt: Date
    var signedIn: Bool
    /// The review queue (nil: signed in, but nothing known yet).
    var reviews: Reviews?
    /// Answers per day for the daily goal (nil: never loaded from the server).
    var habit: Habit?
    /// The title of the page "Continue studying" opens (nil: none). The app looks the page up
    /// again when the link is opened.
    var continueTitle: String?

    enum Source: String, Codable, Sendable {
        /// The server's summary (`GET /api/phone/summary`).
        case server
        /// The review questions saved on the phone (the server could not be reached).
        case phone
    }

    struct Reviews: Codable, Equatable, Sendable {
        /// The moment the numbers below hold at.
        var computedAt: Date
        /// How fresh what they were computed from is (the summary's time, or when the saved
        /// questions were fetched).
        var asOf: Date
        var source: Source
        /// The review day's zone (the cap resets at midnight there).
        var timeZone: String
        /// Questions due at `computedAt` (not capped).
        var dueNow: Int
        var cap: Int
        /// Reviews still allowed on the day of `computedAt`.
        var remainingToday: Int
        /// Reviews answered on the day of `computedAt`.
        var doneToday: Int
        /// When further questions come due, soonest first, all after `computedAt`.
        var upcoming: [Date]
        /// False when `upcoming` lists only the first of more (counts after its last time are
        /// then at least the number shown).
        var upcomingComplete: Bool

        var zone: TimeZone { TimeZone(identifier: timeZone) ?? .current }
    }

    struct DayCount: Codable, Equatable, Sendable {
        var day: LocalDay
        var answers: Int
    }

    struct Habit: Codable, Equatable, Sendable {
        /// Answers per day that meet the goal.
        var goal: Int
        var timeZone: String
        /// The first day of the window (earlier days are not known).
        var firstDay: LocalDay
        /// Study-page and review answers per day, the server's plus those made on the phone that
        /// the server does not count yet. Days not listed had none.
        var days: [DayCount]
        /// When the server's counts were read.
        var asOf: Date

        var zone: TimeZone { TimeZone(identifier: timeZone) ?? .current }

        var answersByDay: [LocalDay: Int] {
            Dictionary(days.map { ($0.day, $0.answers) }, uniquingKeysWith: +)
        }
    }

    static func signedOut(at date: Date) -> WidgetSnapshot {
        WidgetSnapshot(writtenAt: date, signedIn: false, reviews: nil, habit: nil, continueTitle: nil)
    }

    /// The same content, whenever it was written.
    func sameContent(as other: WidgetSnapshot) -> Bool {
        var mine = self
        mine.writtenAt = other.writtenAt
        return mine == other
    }

    struct Invalid: Error, LocalizedError, Equatable {
        var message: String
        var errorDescription: String? { message }
    }

    /// Dates to the millisecond, in UTC.
    private static let dateStyle = Date.ISO8601FormatStyle(includingFractionalSeconds: true)

    static let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .custom { date, encoder in
            var container = encoder.singleValueContainer()
            try container.encode(date.formatted(dateStyle))
        }
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }()

    static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let text = try container.decode(String.self)
            guard let date = try? dateStyle.parse(text) else {
                throw DecodingError.dataCorruptedError(in: container, debugDescription: "not an ISO 8601 date: \(text)")
            }
            return date
        }
        return decoder
    }()

    static func decode(_ data: Data) throws(Invalid) -> WidgetSnapshot {
        struct Header: Decodable { var version: Int }
        let header: Header
        do {
            header = try decoder.decode(Header.self, from: data)
        } catch {
            throw Invalid(message: "The widget's data cannot be read (\(error.localizedDescription)).")
        }
        guard header.version == formatVersion else {
            throw Invalid(message: "The widget's data was written by another version of Airistotle (format \(header.version)). "
                + "Open the app to refresh it.")
        }
        do {
            return try decoder.decode(WidgetSnapshot.self, from: data)
        } catch {
            throw Invalid(message: "The widget's data cannot be read (\(error.localizedDescription)).")
        }
    }

    func encoded() throws -> Data { try Self.encoder.encode(self) }
}

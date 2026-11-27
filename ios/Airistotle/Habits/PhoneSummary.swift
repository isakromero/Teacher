import Foundation

/// `GET /api/phone/summary?tz=<zone>` (`teacher.web.routers.phone`): review counts, upcoming due
/// times, answers per day and the page to resume. Read strictly: a field missing or of the wrong
/// kind makes the whole answer unreadable, and the app says so.
struct PhoneSummary: Sendable, Equatable {
    struct Day: Sendable, Equatable {
        var day: LocalDay
        var pageAnswers: Int
        var reviewAnswers: Int
        var total: Int
    }

    /// The page to resume (`continue`): its title and the app target the server gives.
    struct Resume: Sendable, Equatable {
        var slug: String
        var title: String
        var target: String
    }

    var raw: JSONValue
    var now: Instant
    var zone: TimeZone
    var today: LocalDay
    var dueNow: Int
    var cap: Int
    var doneToday: Int
    var remainingToday: Int
    var itemsTotal: Int
    var nextDueAt: Instant?
    var upcoming: [Instant]
    var upcomingTotal: Int
    var firstDay: LocalDay
    var days: [Day]
    /// The server's streak (days with any answer); the phone's goal streak is `HabitMath`'s.
    var serverStreak: Int
    var resume: Resume?

    struct Invalid: Error, Equatable, LocalizedError {
        var message: String
        var errorDescription: String? { message }
    }

    init(data: Data) throws(Invalid) {
        let value: JSONValue
        do {
            value = try JSONValue(data: data)
        } catch {
            throw Invalid(message: "The server's summary is not JSON (\(error.localizedDescription)).")
        }
        try self.init(value)
    }

    init(_ raw: JSONValue) throws(Invalid) {
        func missing(_ what: String) -> Invalid {
            Invalid(message: "The server's summary has no readable \(what). The server may be newer or older than this app.")
        }
        guard let fields = raw.object, let reviewFields = raw["review"].object else { throw missing("review") }
        guard fields["continue"] != nil else { throw missing("continue") }
        guard reviewFields["next_due_at"] != nil else { throw missing("review.next_due_at") }
        guard let now = Instant(isoString: raw["now"].string ?? "") else { throw missing("now") }
        guard let zone = TimeZone(identifier: raw["tz"].string ?? "") else { throw missing("tz") }
        guard let today = LocalDay(raw["today"].string ?? "") else { throw missing("today") }
        guard let dueNow = raw["due_now"].int else { throw missing("due_now") }
        let review = raw["review"]
        guard let cap = review["cap"].int, let done = review["done_today"].int, let remaining = review["remaining_today"].int,
              let total = review["items_total"].int else { throw missing("review counts") }
        let nextDue: Instant?
        switch review["next_due_at"] {
        case .null: nextDue = nil
        case .string(let text):
            guard let parsed = Instant(isoString: text) else { throw missing("review.next_due_at") }
            nextDue = parsed
        default: throw missing("review.next_due_at")
        }
        let upcomingValue = raw["upcoming_due"]
        guard let times = upcomingValue["times"].array, let upcomingTotal = upcomingValue["total"].int else {
            throw missing("upcoming_due")
        }
        var upcoming: [Instant] = []
        for time in times {
            guard let instant = Instant(isoString: time.string ?? "") else { throw missing("upcoming_due time") }
            upcoming.append(instant)
        }
        let daily = raw["daily_answers"]
        guard let items = daily["items"].array, let firstDay = LocalDay(daily["first_day"].string ?? ""),
              let streak = daily["streak_days"].int else { throw missing("daily_answers") }
        var days: [Day] = []
        for item in items {
            guard let day = LocalDay(item["date"].string ?? ""), let page = item["page_answers"].int,
                  let reviews = item["review_answers"].int, let sum = item["total"].int else { throw missing("daily_answers item") }
            days.append(Day(day: day, pageAnswers: page, reviewAnswers: reviews, total: sum))
        }
        let resume: Resume?
        switch raw["continue"] {
        case .null: resume = nil
        case .object:
            let item = raw["continue"]
            guard let slug = item["artifact"]["slug"].string, let title = item["artifact"]["title"].string,
                  let target = item["url"].string else { throw missing("continue") }
            resume = Resume(slug: slug, title: title, target: target)
        default: throw missing("continue")
        }
        self.raw = raw
        self.now = now
        self.zone = zone
        self.today = today
        self.dueNow = dueNow
        self.cap = cap
        doneToday = done
        remainingToday = remaining
        itemsTotal = total
        nextDueAt = nextDue
        self.upcoming = upcoming.sorted()
        self.upcomingTotal = upcomingTotal
        self.firstDay = firstDay
        self.days = days
        serverStreak = streak
        self.resume = resume
    }

    /// The app target "Continue studying" opens: the server's own study page or lab path, and
    /// nothing else (no other address is ever loaded from it).
    static func resumeTarget(_ target: String) -> String? {
        let parts = target.split(separator: "/", omittingEmptySubsequences: false)
        guard parts.count == 3, parts[0].isEmpty, parts[1] == "artifacts" || parts[1] == "labs",
              let slug = String(parts[2]).removingPercentEncoding, DeepLink.isValidSlug(slug) else { return nil }
        return "/\(parts[1])/\(StudyPageRequests.segment(slug))"
    }
}

import Foundation

/// Which of Aristotle's messages shows, and when. The rules:
///
/// - One message at a time. A message on screen is never cut short by another; the next one
///   waits for it to go, then a short pause (`gap`).
/// - Waiting messages go by priority: a finished page, then today's goal, then the welcome.
///   Equal priorities keep their order.
/// - The welcome is the lesser message: it is dropped when anything else shows or waits as it
///   comes, and a welcome still waiting gives way (is dropped) when anything else comes.
/// - A message shows only where it may (`MascotMessage.places`: the welcome, with its button,
///   only over the main screen).
/// - The same message is not queued twice (the same page, or the goal again).
/// - A message that could not be shown within its `maxWait` is dropped.
/// - A message interrupted by the page becoming unavailable (the keyboard came up, a sheet
///   opened) within `seenAfter` of appearing waits again, ahead of its own priority; one that
///   had been on screen longer counts as seen.
struct MascotQueue {
    static let gap: TimeInterval = 0.8
    static let seenAfter: TimeInterval = 1.5

    struct Waiting: Equatable {
        var message: MascotMessage
        var raisedAt: Date
        var notBefore: Date
    }

    private(set) var showing: MascotMessage?
    private(set) var shownAt: Date?
    private(set) var waiting: [Waiting] = []
    private var pauseUntil: Date?

    /// Queue `message`, to show no earlier than `notBefore`. False when it is dropped.
    @discardableResult
    mutating func raise(_ message: MascotMessage, at now: Date, notBefore: Date? = nil) -> Bool {
        if showing?.kind == message.kind || waiting.contains(where: { $0.message.kind == message.kind }) { return false }
        if message.kind == .welcomeBack {
            guard showing == nil, waiting.isEmpty else { return false }
        } else {
            waiting.removeAll { $0.message.kind == .welcomeBack }
        }
        let entry = Waiting(message: message, raisedAt: now, notBefore: max(now, notBefore ?? now))
        let index = waiting.firstIndex { $0.message.priority < message.priority } ?? waiting.endIndex
        waiting.insert(entry, at: index)
        return true
    }

    /// The message to show now in `place` (nil: no place is free), if there is one; it becomes
    /// `showing`. Messages that waited too long are dropped.
    mutating func next(at now: Date, place: MascotSurface?) -> MascotMessage? {
        waiting.removeAll { now.timeIntervalSince($0.raisedAt) > $0.message.maxWait }
        guard showing == nil, let place else { return nil }
        if let pauseUntil, now < pauseUntil { return nil }
        guard let index = waiting.firstIndex(where: { $0.notBefore <= now && $0.message.places.contains(place) }) else { return nil }
        let entry = waiting.remove(at: index)
        showing = entry.message
        shownAt = now
        return entry.message
    }

    /// The message on screen went (its time was up, or the reader dismissed it).
    mutating func finished(_ id: UUID, at now: Date) {
        guard showing?.id == id else { return }
        showing = nil
        shownAt = nil
        pauseUntil = now.addingTimeInterval(Self.gap)
    }

    /// The place the message showed in went away while it was on screen.
    mutating func interrupted(at now: Date) {
        guard let message = showing else { return }
        let seen = shownAt.map { now.timeIntervalSince($0) >= Self.seenAfter } ?? true
        showing = nil
        shownAt = nil
        guard !seen else { return }
        // Ahead of the waiting messages of its own priority.
        let index = waiting.firstIndex { $0.message.priority <= message.priority } ?? waiting.endIndex
        waiting.insert(Waiting(message: message, raisedAt: now, notBefore: now), at: index)
    }

    /// Nothing more is said (the setting was turned off, or the account signed out).
    mutating func clear() {
        showing = nil
        shownAt = nil
        waiting = []
        pauseUntil = nil
    }

    /// When `next` may next have something to show, if that is later than `now` (a message
    /// waiting for its moment or for the pause after the last one).
    func nextWake(after now: Date) -> Date? {
        guard showing == nil, !waiting.isEmpty else { return nil }
        let earliest = waiting.map(\.notBefore).min() ?? now
        let wake = max(earliest, pauseUntil ?? earliest)
        return wake > now ? wake : nil
    }
}

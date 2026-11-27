import Foundation

/// One thing Aristotle says: a line or two in a speech bubble, maybe with a button.
struct MascotMessage: Identifiable, Equatable, Sendable {
    enum Kind: Equatable, Sendable {
        /// A study page was finished (every gate cleared) by an answer made in the app.
        case pageFinished(slug: String)
        /// Today's goal was met (the "goal met" note, said by Aristotle).
        case goalMet
        /// The app was opened after `WelcomeBack.threshold` or more away.
        case welcomeBack
    }

    /// What the bubble's button does.
    enum Action: Equatable, Sendable {
        case startReview
        case continueStudying

        var label: String {
            switch self {
            case .startReview: "Start review"
            case .continueStudying: "Continue"
            }
        }
    }

    let id: UUID
    var kind: Kind
    /// One or two short lines; the first is the headline.
    var lines: [String]
    var action: Action?

    init(id: UUID = UUID(), kind: Kind, lines: [String], action: Action? = nil) {
        self.id = id
        self.kind = kind
        self.lines = lines
        self.action = action
    }

    /// What VoiceOver announces and reads for the overlay.
    var spoken: String { lines.joined(separator: " ") }

    /// Which message gives way to which (`MascotQueue`): a finished page first, then the goal,
    /// then the welcome.
    var priority: Int {
        switch kind {
        case .pageFinished: 3
        case .goalMet: 2
        case .welcomeBack: 1
        }
    }

    /// How long it stays on screen (twice as long with VoiceOver, `MascotCenter`).
    var duration: TimeInterval {
        switch kind {
        case .pageFinished: 5
        case .goalMet: 4
        case .welcomeBack: action == nil ? 5 : 7
        }
    }

    /// How long it may wait for its turn (the keyboard is up, a sheet covers the page, a review
    /// question is on screen) before it is no longer worth saying.
    var maxWait: TimeInterval {
        switch kind {
        case .pageFinished, .goalMet: 30 * 60
        case .welcomeBack: 20
        }
    }

    /// Where it may show: the welcome (with its button to start a review or open a page) only
    /// over the main screen; the others also over a review's summary.
    var places: Set<MascotSurface> {
        switch kind {
        case .pageFinished, .goalMet: [.browser, .review]
        case .welcomeBack: [.browser]
        }
    }

    var performance: MascotPerformance {
        switch kind {
        case .pageFinished: .cheer
        case .goalMet: .shortCheer
        case .welcomeBack: .wave
        }
    }
}

/// Every word Aristotle says. Plain and warm: no guilt about time away or lost streaks, no
/// quotations, no exclamation marks.
enum MascotText {
    /// The second line after finishing a page, one of these, the same for a page every time
    /// (`stableIndex`).
    static let finishedLines = [
        "Every checkpoint is cleared.",
        "Well done.",
        "Nicely worked through.",
        "That one is yours now."
    ]

    /// "You finished Waves and superposition." / "Well done."
    static func pageFinished(slug: String, title: String?) -> MascotMessage {
        let name = title.map(bare).flatMap { $0.isEmpty ? nil : $0 }
        let first = name.map { "You finished \($0)." } ?? "You finished this study page."
        return MascotMessage(kind: .pageFinished(slug: slug), lines: [first, finishedLines[stableIndex(slug, count: finishedLines.count)]])
    }

    /// "Today's goal is met." / "11 answers today."
    static func goalMet(answers: Int) -> MascotMessage {
        MascotMessage(kind: .goalMet, lines: ["Today's goal is met.", answers == 1 ? "1 answer today." : "\(answers) answers today."])
    }

    /// The welcome, from what is due now and the page to resume (`WelcomeBack`).
    static func welcomeBack(due: Int?, atLeast: Bool, resumeTitle: String?) -> MascotMessage {
        let first = "Welcome back."
        let resume = resumeTitle.map(bare).flatMap { $0.isEmpty ? nil : $0 }
        if let due, due > 0 {
            let count = atLeast ? "At least \(due)" : "\(due)"
            let line = due == 1 && !atLeast ? "1 review is due." : "\(count) reviews are due."
            return MascotMessage(kind: .welcomeBack, lines: [first, line], action: .startReview)
        }
        if let resume {
            let line = due == nil ? "Pick up \(resume)?" : "Nothing is due yet. Pick up \(resume)?"
            return MascotMessage(kind: .welcomeBack, lines: [first, line], action: .continueStudying)
        }
        if due != nil { return MascotMessage(kind: .welcomeBack, lines: [first, "Nothing is due yet."]) }
        return MascotMessage(kind: .welcomeBack, lines: [first])
    }

    /// A title without the punctuation it may end with, so a sentence can end it.
    static func bare(_ title: String) -> String {
        var text = title.trimmingCharacters(in: .whitespacesAndNewlines)
        while let last = text.last, ".!?:;,".contains(last) { text.removeLast() }
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// The same index for the same text on every launch and device (FNV-1a; Swift's own
    /// `hashValue` changes from one launch to the next).
    static func stableIndex(_ text: String, count: Int) -> Int {
        guard count > 0 else { return 0 }
        var hash: UInt64 = 0xCBF2_9CE4_8422_2325
        for byte in text.utf8 {
            hash ^= UInt64(byte)
            hash = hash &* 0x0000_0100_0000_01B3
        }
        return Int(hash % UInt64(count))
    }
}

/// How the figure moves while a message shows.
enum MascotPerformance: Equatable, Sendable {
    /// Arms up and small hops, then standing (a finished page).
    case cheer
    /// The same, briefer (today's goal).
    case shortCheer
    /// A wave, then standing (welcome back).
    case wave
}

/// Which frame shows `elapsed` seconds into a performance. With Reduce Motion: one still pose
/// (no hop, no wave), the overlay fading in and out.
enum MascotAnimation {
    /// Frames of a cheer or a wave last this long.
    static let step: TimeInterval = 0.2
    /// The eyes close for `blinkLength` once every `blinkEvery` while standing.
    static let blinkEvery: TimeInterval = 2.6
    static let blinkLength: TimeInterval = 0.14

    static func pose(_ performance: MascotPerformance, elapsed: TimeInterval, reduceMotion: Bool) -> MascotPose {
        if reduceMotion {
            switch performance {
            case .cheer, .shortCheer: return .cheer
            case .wave: return .wave1
            }
        }
        let time = max(0, elapsed)
        let beat = Int(time / step)
        switch performance {
        case .cheer:
            if time < 8 * step { return beat.isMultiple(of: 2) ? .cheer : .hop }
            if time < 12 * step { return .cheer }
            return standing(time - 12 * step)
        case .shortCheer:
            if time < 4 * step { return beat.isMultiple(of: 2) ? .cheer : .hop }
            if time < 7 * step { return .cheer }
            return standing(time - 7 * step)
        case .wave:
            // A slower wave: the hand moves every 1.5 steps.
            if time < 9 * step { return Int(time / (1.5 * step)).isMultiple(of: 2) ? .wave1 : .wave2 }
            return standing(time - 9 * step)
        }
    }

    /// Standing, with a blink now and then (the first one a moment after settling).
    static func standing(_ time: TimeInterval) -> MascotPose {
        let phase = (time + blinkEvery / 2).truncatingRemainder(dividingBy: blinkEvery)
        return phase >= blinkEvery - blinkLength ? .blink : .idle
    }
}

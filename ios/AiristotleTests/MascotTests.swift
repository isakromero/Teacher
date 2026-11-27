import XCTest
@testable import Airistotle

private func freshDefaults(_ name: String = #function) -> UserDefaults {
    UserDefaults(suiteName: "MascotTests-\(name)-\(UUID().uuidString)")! // swiftlint:disable:this force_unwrapping
}

private let start = Date(timeIntervalSince1970: 1_790_000_000)

// MARK: - The sprite

final class MascotSpriteTests: XCTestCase {
    /// The icon (`static/img/favicon.svg`), rows 1 to 12: laurel, hair, face, beard.
    private let icon = [
        ".....GGGGGG.....",
        "....GHHHHHHG....",
        "...GHHHHHHHHG...",
        "...HHSSSSSSHH...",
        "...HSSSSSSSSH...",
        "...HSESSSSESH...",
        "...HSSSSNSSSH...",
        "...HBSSSNNSBH...",
        "...BBBBNNBBBB...",
        "...BBBBEEBBBB...",
        "....bBBBBBBb....",
        ".....bBBBBb....."
    ]
    /// The icon's colours, from the SVG.
    private let iconColors: [Character: UInt32] = [
        "G": 0x7FB04A, "H": 0xD8D2C4, "S": 0xE6B088, "E": 0x2A2019, "N": 0xC48A60, "B": 0xF3EFE6, "b": 0xC2BBAB
    ]

    func testEveryFrameIs32By32InThePalette() {
        for pose in MascotPose.allCases {
            let rows = MascotSprite.rows(pose)
            XCTAssertEqual(rows.count, MascotSprite.height, "\(pose)")
            for (index, row) in rows.enumerated() {
                XCTAssertEqual(row.count, MascotSprite.width, "\(pose) row \(index)")
                for key in row where key != "." {
                    XCTAssertNotNil(MascotSprite.palette[key], "\(pose) row \(index): \(key)")
                }
            }
            let opaque = rows.joined().filter { $0 != "." }.count
            XCTAssertEqual(MascotSprite.runs(pose).reduce(0) { $0 + $1.length }, opaque, "\(pose): the runs cover every pixel")
        }
    }

    func testTheFaceBeardAndLaurelAreTheIconsPixelForPixel() {
        for pose in [MascotPose.idle, .wave1, .wave2, .cheer] {
            let rows = MascotSprite.rows(pose).map(Array.init)
            for (row, line) in icon.enumerated() {
                for (column, key) in line.enumerated() where key != "." {
                    XCTAssertEqual(rows[row + 5][column + 8], key, "\(pose): icon pixel (\(column), \(row + 1))")
                }
            }
        }
        for (key, value) in iconColors {
            XCTAssertEqual(MascotSprite.colors[key], value, "the icon's \(key)")
        }
    }

    func testBlinkClosesOnlyTheEyesAndTheHopIsTheCheerTwoPixelsUp() {
        let idle = MascotSprite.rows(.idle).map(Array.init)
        let blink = MascotSprite.rows(.blink).map(Array.init)
        var changed: [String] = []
        for row in 0..<32 {
            for column in 0..<32 where idle[row][column] != blink[row][column] { changed.append("\(column),\(row):\(blink[row][column])") }
        }
        XCTAssertEqual(changed, ["13,10:N", "18,10:N"])
        // The figure (not the sparkles or the shadow) is the same, two rows higher.
        let cheer = MascotSprite.rows(.cheer).map(Array.init)
        let hop = MascotSprite.rows(.hop).map(Array.init)
        for row in 4..<29 {
            for column in 4..<28 where !"wkK.".contains(cheer[row][column]) {
                XCTAssertEqual(hop[row - 2][column], cheer[row][column], "(\(column), \(row))")
            }
        }
    }

    func testTheFigureHasAnOutlineAgainstTheLightPage() {
        // Every robe or beard pixel at the figure's edge touches the outline, not the page.
        for pose in MascotPose.allCases {
            let rows = MascotSprite.rows(pose).map(Array.init)
            for row in 0..<32 {
                for column in 0..<32 where "RBHqb".contains(rows[row][column]) {
                    for (across, down) in [(1, 0), (-1, 0), (0, 1), (0, -1)] {
                        let (next, below) = (column + across, row + down)
                        guard (0..<32).contains(next), (0..<32).contains(below) else { return XCTFail("\(pose) touches the edge") }
                        XCTAssertNotEqual(rows[below][next], ".", "\(pose): (\(column), \(row)) meets the page without an outline")
                    }
                }
            }
        }
    }
}

// MARK: - Animation and words

final class MascotAnimationTests: XCTestCase {
    func testCheerHopsThenStandsAndBlinks() {
        let poses = stride(from: 0.0, to: 12, by: 0.05).map { MascotAnimation.pose(.cheer, elapsed: $0, reduceMotion: false) }
        XCTAssertEqual(MascotAnimation.pose(.cheer, elapsed: 0, reduceMotion: false), .cheer)
        XCTAssertEqual(MascotAnimation.pose(.cheer, elapsed: 0.25, reduceMotion: false), .hop)
        XCTAssertTrue(poses.contains(.hop))
        XCTAssertFalse(Array(poses.dropFirst(60)).contains(.hop), "the hops end")
        XCTAssertTrue(poses.contains(.blink), "standing, he blinks now and then")
        XCTAssertGreaterThan(poses.filter { $0 == .idle }.count, poses.filter { $0 == .blink }.count * 8, "rarely")
    }

    func testWaveWavesThenStands() {
        let poses = stride(from: 0.0, to: 6, by: 0.05).map { MascotAnimation.pose(.wave, elapsed: $0, reduceMotion: false) }
        XCTAssertEqual(poses.first, .wave1)
        XCTAssertTrue(poses.contains(.wave2))
        XCTAssertEqual(Set(poses.suffix(40)).subtracting([.idle, .blink]), [])
    }

    func testReduceMotionIsOneStillPose() {
        for time in stride(from: 0.0, to: 10, by: 0.1) {
            XCTAssertEqual(MascotAnimation.pose(.cheer, elapsed: time, reduceMotion: true), .cheer)
            XCTAssertEqual(MascotAnimation.pose(.shortCheer, elapsed: time, reduceMotion: true), .cheer)
            XCTAssertEqual(MascotAnimation.pose(.wave, elapsed: time, reduceMotion: true), .wave1)
        }
    }
}

final class MascotTextTests: XCTestCase {
    func testAFinishedPageIsNamedAndTheSecondLineIsStable() {
        let message = MascotText.pageFinished(slug: "waves", title: "Waves and superposition")
        XCTAssertEqual(message.lines.first, "You finished Waves and superposition.")
        XCTAssertEqual(message.lines.count, 2)
        XCTAssertTrue(MascotText.finishedLines.contains(message.lines[1]))
        XCTAssertEqual(MascotText.pageFinished(slug: "waves", title: "Other").lines[1], message.lines[1], "chosen by the slug")
        XCTAssertEqual(MascotText.stableIndex("waves", count: 4), 1, "the same on every launch (FNV-1a)")
        XCTAssertEqual(MascotText.stableIndex("optics", count: 4), 3)
        XCTAssertEqual(MascotText.pageFinished(slug: "waves", title: "Waves and superposition").lines[1], "Well done.")
        let used = Set(["a", "b", "c", "d", "e", "f", "g", "h", "relativity", "optics"].map { MascotText.stableIndex($0, count: 4) })
        XCTAssertGreaterThan(used.count, 2, "the lines vary between pages")
        XCTAssertEqual(MascotText.pageFinished(slug: "q", title: "What is energy?").lines[0], "You finished What is energy.")
        XCTAssertEqual(MascotText.pageFinished(slug: "q", title: nil).lines[0], "You finished this study page.")
        XCTAssertEqual(MascotText.pageFinished(slug: "q", title: "  ").lines[0], "You finished this study page.")
        XCTAssertEqual(message.kind, .pageFinished(slug: "waves"))
        XCTAssertNil(message.action)
    }

    func testTheGoal() {
        XCTAssertEqual(MascotText.goalMet(answers: 11).lines, ["Today's goal is met.", "11 answers today."])
        XCTAssertEqual(MascotText.goalMet(answers: 1).lines[1], "1 answer today.")
    }

    func testWelcomeBack() {
        let due = MascotText.welcomeBack(due: 7, atLeast: false, resumeTitle: "Waves")
        XCTAssertEqual(due.lines, ["Welcome back.", "7 reviews are due."])
        XCTAssertEqual(due.action, .startReview)
        XCTAssertEqual(due.action?.label, "Start review")
        XCTAssertEqual(MascotText.welcomeBack(due: 1, atLeast: false, resumeTitle: nil).lines[1], "1 review is due.")
        XCTAssertEqual(MascotText.welcomeBack(due: 20, atLeast: true, resumeTitle: nil).lines[1], "At least 20 reviews are due.")
        let resume = MascotText.welcomeBack(due: 0, atLeast: false, resumeTitle: "Waves and superposition")
        XCTAssertEqual(resume.lines, ["Welcome back.", "Nothing is due yet. Pick up Waves and superposition?"])
        XCTAssertEqual(resume.action, .continueStudying)
        XCTAssertEqual(MascotText.welcomeBack(due: 0, atLeast: false, resumeTitle: nil).lines, ["Welcome back.", "Nothing is due yet."])
        XCTAssertNil(MascotText.welcomeBack(due: 0, atLeast: false, resumeTitle: nil).action)
        XCTAssertEqual(MascotText.welcomeBack(due: nil, atLeast: false, resumeTitle: "Optics").lines, ["Welcome back.", "Pick up Optics?"])
        XCTAssertEqual(MascotText.welcomeBack(due: nil, atLeast: false, resumeTitle: nil).lines, ["Welcome back."])
    }

    func testNoWordIsShoutedOrQuoted() {
        var all = MascotText.finishedLines
        all += MascotText.goalMet(answers: 3).lines
        for due in [nil, 0, 1, 9] as [Int?] {
            for title in [nil, "Waves"] as [String?] { all += MascotText.welcomeBack(due: due, atLeast: false, resumeTitle: title).lines }
        }
        for line in all {
            XCTAssertFalse(line.contains("!"), line)
            XCTAssertFalse(line.contains("\""), line)
            XCTAssertFalse(line.lowercased().contains("streak"), line)
            XCTAssertFalse(line.lowercased().contains("miss"), line)
        }
    }
}

// MARK: - Which message shows when

final class MascotQueueTests: XCTestCase {
    private func page(_ slug: String = "waves") -> MascotMessage { MascotText.pageFinished(slug: slug, title: nil) }
    private var goal: MascotMessage { MascotText.goalMet(answers: 10) }
    private var welcome: MascotMessage { MascotText.welcomeBack(due: 3, atLeast: false, resumeTitle: nil) }

    func testOneAtATimeByPriorityWithAPause() throws {
        var queue = MascotQueue()
        XCTAssertTrue(queue.raise(goal, at: start))
        XCTAssertTrue(queue.raise(page(), at: start))
        let first = queue.next(at: start, place: .browser)
        XCTAssertEqual(first?.kind, .pageFinished(slug: "waves"), "the page before the goal")
        XCTAssertNil(queue.next(at: start, place: .browser), "one at a time")
        queue.finished(try XCTUnwrap(first).id, at: start.addingTimeInterval(5))
        XCTAssertNil(queue.next(at: start.addingTimeInterval(5.1), place: .browser), "a short pause between two")
        XCTAssertEqual(queue.nextWake(after: start.addingTimeInterval(5.1)), start.addingTimeInterval(5 + MascotQueue.gap))
        XCTAssertEqual(queue.next(at: start.addingTimeInterval(6), place: .browser)?.kind, .goalMet)
    }

    func testTheWelcomeGivesWayToEverythingElse() {
        var queue = MascotQueue()
        XCTAssertTrue(queue.raise(page(), at: start))
        XCTAssertFalse(queue.raise(welcome, at: start), "something waits: the welcome is dropped")
        _ = queue.next(at: start, place: .browser)
        XCTAssertFalse(queue.raise(welcome, at: start), "something shows: the welcome is dropped")

        var waitingWelcome = MascotQueue()
        XCTAssertTrue(waitingWelcome.raise(welcome, at: start, notBefore: start.addingTimeInterval(1.5)))
        XCTAssertTrue(waitingWelcome.raise(page(), at: start.addingTimeInterval(1)))
        XCTAssertEqual(waitingWelcome.waiting.map(\.message.kind), [.pageFinished(slug: "waves")], "the waiting welcome is dropped")

        var showingWelcome = MascotQueue()
        showingWelcome.raise(welcome, at: start)
        XCTAssertEqual(showingWelcome.next(at: start, place: .browser)?.kind, .welcomeBack)
        XCTAssertTrue(showingWelcome.raise(page(), at: start.addingTimeInterval(1)))
        XCTAssertEqual(showingWelcome.showing?.kind, .welcomeBack, "never cut short")
        XCTAssertNil(showingWelcome.next(at: start.addingTimeInterval(1), place: .browser))
    }

    func testTheWelcomeWaitsForTheScreenToSettleAndOnlyShowsOverThePage() {
        var queue = MascotQueue()
        queue.raise(welcome, at: start, notBefore: start.addingTimeInterval(1.5))
        XCTAssertNil(queue.next(at: start.addingTimeInterval(1), place: .browser))
        XCTAssertEqual(queue.nextWake(after: start.addingTimeInterval(1)), start.addingTimeInterval(1.5))
        XCTAssertNil(queue.next(at: start.addingTimeInterval(2), place: .review), "not over a review's summary")
        XCTAssertEqual(queue.next(at: start.addingTimeInterval(2), place: .browser)?.kind, .welcomeBack)
        var pageQueue = MascotQueue()
        pageQueue.raise(page(), at: start)
        XCTAssertNil(pageQueue.next(at: start, place: nil), "nowhere free")
        XCTAssertEqual(pageQueue.next(at: start, place: .review)?.kind, .pageFinished(slug: "waves"))
    }

    func testTheSameMessageIsNotQueuedTwiceAndOldOnesLapse() {
        var queue = MascotQueue()
        XCTAssertTrue(queue.raise(page(), at: start))
        XCTAssertFalse(queue.raise(page(), at: start))
        XCTAssertTrue(queue.raise(page("optics"), at: start))
        XCTAssertTrue(queue.raise(goal, at: start))
        XCTAssertFalse(queue.raise(goal, at: start))
        XCTAssertEqual(queue.waiting.map(\.message.kind), [.pageFinished(slug: "waves"), .pageFinished(slug: "optics"), .goalMet])
        var lapsing = MascotQueue()
        lapsing.raise(welcome, at: start)
        XCTAssertNil(lapsing.next(at: start.addingTimeInterval(21), place: .browser), "a welcome not shown within 20 s lapses")
        XCTAssertTrue(lapsing.waiting.isEmpty)
        lapsing.raise(page(), at: start)
        XCTAssertNotNil(lapsing.next(at: start.addingTimeInterval(25 * 60), place: .browser), "a finished page waits longer")
    }

    func testAnInterruptedMessageComesBackUnlessItWasSeen() {
        var queue = MascotQueue()
        queue.raise(page(), at: start)
        queue.raise(goal, at: start)
        _ = queue.next(at: start, place: .browser)
        queue.interrupted(at: start.addingTimeInterval(0.5))
        XCTAssertEqual(queue.waiting.map(\.message.kind), [.pageFinished(slug: "waves"), .goalMet], "back, ahead of the goal")
        _ = queue.next(at: start.addingTimeInterval(1), place: .browser)
        queue.interrupted(at: start.addingTimeInterval(3))
        XCTAssertEqual(queue.waiting.map(\.message.kind), [.goalMet], "on screen for 2 s: seen")
        queue.clear()
        XCTAssertTrue(queue.waiting.isEmpty)
        XCTAssertNil(queue.showing)
    }
}

// MARK: - A finished page

final class PageCompletionWatcherTests: XCTestCase {
    private func report(_ kind: ReadingReport.Kind, slug: String = "waves", namespace: String = "ns") -> ReadingReport {
        ReadingReport(namespace: namespace, slug: slug, kind: kind)
    }

    func testAnAnswerThatFinishesAPageSeenIncompleteIsCongratulatedOnce() {
        var watcher = PageCompletionWatcher()
        watcher.load("ns", slugs: [])
        XCTAssertNil(watcher.observe(report(.title("Waves and superposition"))))
        XCTAssertNil(watcher.observe(report(.state(complete: false))))
        XCTAssertNil(watcher.observe(report(.answered(complete: false))), "a gate cleared, others left")
        XCTAssertEqual(watcher.observe(report(.answered(complete: true))),
                       .init(namespace: "ns", slug: "waves", title: "Waves and superposition"))
        XCTAssertEqual(watcher.congratulated["ns"], ["waves"])
        // Opened again and answered again (a revealed gate takes attempts): nothing more.
        XCTAssertNil(watcher.observe(report(.state(complete: false))))
        XCTAssertNil(watcher.observe(report(.answered(complete: true))), "once per page")
    }

    func testAPageCompleteWhenOpenedOrNeverSeenIncompleteGivesNothing() {
        var watcher = PageCompletionWatcher()
        watcher.load("ns", slugs: [])
        XCTAssertNil(watcher.observe(report(.state(complete: true))))
        XCTAssertNil(watcher.observe(report(.answered(complete: true))), "already complete when opened")
        XCTAssertNil(watcher.observe(report(.answered(complete: true), slug: "optics")), "never seen incomplete")
        XCTAssertNil(watcher.observe(report(.answered(complete: false), slug: "unseen")))
        XCTAssertEqual(watcher.observe(report(.answered(complete: true), slug: "unseen"))?.slug, "unseen",
                       "seen incomplete through an answer's reply counts")
    }

    func testPagesCongratulatedBeforeAndUnloadedAccountsGiveNothing() {
        var watcher = PageCompletionWatcher()
        watcher.load("ns", slugs: ["waves"])
        XCTAssertNil(watcher.observe(report(.state(complete: false))))
        XCTAssertNil(watcher.observe(report(.answered(complete: true))), "congratulated before (another launch)")
        XCTAssertNil(watcher.observe(report(.state(complete: false), namespace: "other")))
        XCTAssertNil(watcher.observe(report(.answered(complete: true), namespace: "other")), "the account's pages could not be read")
        watcher.load("other", slugs: [])
        XCTAssertNil(watcher.observe(report(.state(complete: false), slug: "waves", namespace: "other")))
        XCTAssertEqual(watcher.observe(report(.answered(complete: true), slug: "waves", namespace: "other"))?.namespace, "other",
                       "each account has its own pages")
        XCTAssertNil(watcher.observe(report(.answered(complete: true), slug: "waves", namespace: "other"))?.title)
    }
}

// MARK: - Welcome back

final class WelcomeBackTests: XCTestCase {
    private func snapshot(due: Int, continueTitle: String? = "Waves") -> WidgetSnapshot {
        let reviews = WidgetSnapshot.Reviews(computedAt: start, asOf: start, source: .server, timeZone: "Europe/Stockholm", dueNow: due,
                                             cap: 20, remainingToday: 20, doneToday: 0, upcoming: [], upcomingComplete: true)
        return WidgetSnapshot(writtenAt: start, signedIn: true, reviews: reviews, habit: nil, continueTitle: continueTitle)
    }

    func testOnlyAfterThreeDaysAwayAndSignedIn() {
        let now = start.addingTimeInterval(WelcomeBack.threshold)
        XCTAssertEqual(WelcomeBack.threshold, 3 * 24 * 3600)
        XCTAssertNotNil(WelcomeBack.message(lastActive: start, now: now, signedIn: true, snapshot: snapshot(due: 2)))
        XCTAssertNil(WelcomeBack.message(lastActive: start.addingTimeInterval(1), now: now, signedIn: true, snapshot: snapshot(due: 2)))
        XCTAssertNil(WelcomeBack.message(lastActive: nil, now: now, signedIn: true, snapshot: snapshot(due: 2)), "the first launch")
        XCTAssertNil(WelcomeBack.message(lastActive: start, now: now, signedIn: false, snapshot: snapshot(due: 2)), "signed out")
        XCTAssertNil(WelcomeBack.message(lastActive: now.addingTimeInterval(9 * 86_400), now: now, signedIn: true, snapshot: nil),
                     "a clock that went backwards")
    }

    func testTheMessageComesFromTheSnapshot() {
        let now = start.addingTimeInterval(5 * 86_400)
        let due = WelcomeBack.message(lastActive: start, now: now, signedIn: true, snapshot: snapshot(due: 7))
        XCTAssertEqual(due?.lines, ["Welcome back.", "7 reviews are due."])
        XCTAssertEqual(due?.action, .startReview)
        let none = WelcomeBack.message(lastActive: start, now: now, signedIn: true, snapshot: snapshot(due: 0))
        XCTAssertEqual(none?.lines, ["Welcome back.", "Nothing is due yet. Pick up Waves?"])
        XCTAssertEqual(none?.action, .continueStudying)
        let unknown = WelcomeBack.message(lastActive: start, now: now, signedIn: true, snapshot: nil)
        XCTAssertEqual(unknown?.lines, ["Welcome back."])
        XCTAssertNil(unknown?.action)
    }
}

// MARK: - What is remembered

final class MascotStoreTests: XCTestCase {
    func testDefaultsRoundTripAndUnreadableValuesAreErrors() throws {
        let defaults = freshDefaults()
        let store = MascotStore(defaults: defaults)
        XCTAssertTrue(store.enabled, "on by default")
        store.enabled = false
        XCTAssertFalse(MascotStore(defaults: defaults).enabled)
        XCTAssertNil(try store.lastActive())
        store.setLastActive(start)
        XCTAssertEqual(try store.lastActive(), start)
        XCTAssertEqual(try store.congratulated("ns"), [])
        try store.setCongratulated(["waves", "optics"], namespace: "ns")
        XCTAssertEqual(try store.congratulated("ns"), ["waves", "optics"])
        XCTAssertEqual(try store.congratulated("other"), [], "per server and account")

        defaults.set("garbage", forKey: MascotStore.congratulatedPrefix + "ns")
        XCTAssertThrowsError(try store.congratulated("ns"))
        XCTAssertEqual(defaults.string(forKey: MascotStore.congratulatedPrefix + "ns"), "garbage", "left as it is")
        defaults.set("yesterday", forKey: MascotStore.lastActiveKey)
        XCTAssertThrowsError(try store.lastActive())
        store.forgetAll()
        XCTAssertTrue(store.enabled)
        XCTAssertNil(try store.lastActive())
        XCTAssertEqual(try store.congratulated("ns"), [])
    }
}

// MARK: - The centre

@MainActor
final class MascotCenterTests: XCTestCase {
    private func report(_ kind: ReadingReport.Kind, slug: String = "waves") -> ReadingReport {
        ReadingReport(namespace: "ns", slug: slug, kind: kind)
    }

    private func finish(_ center: MascotCenter, slug: String = "waves") {
        center.readingProgress(report(.title("Waves and superposition"), slug: slug))
        center.readingProgress(report(.state(complete: false), slug: slug))
        center.readingProgress(report(.answered(complete: true), slug: slug))
    }

    func testAFinishedPageShowsOnceAcrossLaunchesWhereThereIsRoom() throws {
        let defaults = freshDefaults()
        let center = MascotCenter(store: MascotStore(defaults: defaults))
        center.voiceOverRunning = { false }
        finish(center)
        XCTAssertNil(center.current, "nowhere to show it yet")
        center.setAvailable(.browser, true)
        let shown = try XCTUnwrap(center.current)
        XCTAssertEqual(shown.lines.first, "You finished Waves and superposition.")
        XCTAssertEqual(center.surface, .browser)
        XCTAssertEqual(try MascotStore(defaults: defaults).congratulated("ns"), ["waves"])
        center.dismiss(shown.id)
        XCTAssertNil(center.current)

        let relaunched = MascotCenter(store: MascotStore(defaults: defaults))
        relaunched.setAvailable(.browser, true)
        finish(relaunched)
        XCTAssertNil(relaunched.current, "not again after a relaunch (or signing in again)")
    }

    func testTheKeyboardOrASheetPutsItOffAndTheSettingSilencesIt() throws {
        let center = MascotCenter(store: MascotStore(defaults: freshDefaults()))
        center.setAvailable(.browser, true)
        finish(center)
        XCTAssertNotNil(center.current)
        center.setAvailable(.browser, false)
        XCTAssertNil(center.current, "the keyboard came up")
        center.setAvailable(.browser, true)
        XCTAssertNotNil(center.current, "shown again: it had only just appeared")
        center.enabled = false
        XCTAssertNil(center.current)
        finish(center, slug: "optics")
        XCTAssertNil(center.current, "off: nothing is said")
        XCTAssertFalse(center.goalMet(answers: 10), "off: the plain goal note shows instead")
        center.enabled = true
        XCTAssertTrue(center.goalMet(answers: 10))
        XCTAssertEqual(center.current?.kind, .goalMet)
    }

    func testAnUnreadableMemoryIsSaidAndNothingIsRepeated() throws {
        let defaults = freshDefaults()
        defaults.set(Data("not json".utf8), forKey: MascotStore.congratulatedPrefix + "ns")
        let center = MascotCenter(store: MascotStore(defaults: defaults))
        center.setAvailable(.browser, true)
        finish(center)
        XCTAssertNil(center.current, "it cannot know whether this page was congratulated before")
        XCTAssertEqual(center.problem, MascotStore.Unreadable(what: "the pages you finished").localizedDescription)
        XCTAssertEqual(defaults.data(forKey: MascotStore.congratulatedPrefix + "ns"), Data("not json".utf8), "not replaced")
        center.forgetUnreadable()
        XCTAssertNil(center.problem)
        finish(center, slug: "optics")
        XCTAssertEqual(center.current?.kind, .pageFinished(slug: "optics"))
    }

    func testWelcomeBackRecordsActivityAndWaitsForTheScreenToSettle() async throws {
        let defaults = freshDefaults()
        let store = MascotStore(defaults: defaults)
        let center = MascotCenter(store: store)
        XCTAssertNil(center.noteActive(), "the first launch")
        store.setLastActive(Date().addingTimeInterval(-4 * 86_400))
        let before = try XCTUnwrap(center.noteActive())
        XCTAssertLessThan(Date().timeIntervalSince(try XCTUnwrap(try store.lastActive())), 5, "now is kept")
        center.setAvailable(.browser, true)
        center.welcomeBack(lastActive: before, signedIn: true, snapshot: nil)
        XCTAssertNil(center.current, "not during launch")
        for _ in 0..<40 where center.current == nil { try await Task.sleep(for: .milliseconds(100)) }
        XCTAssertEqual(center.current?.lines, ["Welcome back."])
    }
}

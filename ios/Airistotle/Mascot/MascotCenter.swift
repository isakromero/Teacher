import Foundation
import Observation
import UIKit

/// Where a message can show: over the main screen, or over the native review's summary.
enum MascotSurface: Hashable, Sendable {
    case browser
    case review
}

/// Aristotle's messages: what triggers them (a finished page, today's goal, coming back after
/// a while), which one shows when (`MascotQueue`), and where. The views say when they are free
/// to show one (`setAvailable`): the main screen when nothing covers it and the keyboard is
/// down, the review only on its summary. A message shows for its `duration` (twice as long with
/// VoiceOver) unless dismissed sooner.
@MainActor
@Observable
final class MascotCenter {
    /// The message on screen, and where.
    private(set) var current: MascotMessage?
    private(set) var surface: MascotSurface?
    /// When `current` appeared (its animation starts there).
    private(set) var shownAt: Date?
    /// Why something Aristotle remembers cannot be read or kept (shown in Settings).
    private(set) var problem: String?

    @ObservationIgnored let store: MascotStore
    @ObservationIgnored private var queue = MascotQueue()
    @ObservationIgnored private var watcher = PageCompletionWatcher()
    @ObservationIgnored private var available: Set<MascotSurface> = []
    @ObservationIgnored private var unreadable: Set<String> = []
    @ObservationIgnored private var wakeTask: Task<Void, Never>?
    @ObservationIgnored private var dismissTask: Task<Void, Never>?
    @ObservationIgnored private let clock: () -> Date
    @ObservationIgnored var voiceOverRunning: () -> Bool = { UIAccessibility.isVoiceOverRunning }

    init(store: MascotStore, clock: @escaping () -> Date = { Date() }) {
        self.store = store
        self.clock = clock
    }

    /// "Show Aristotle". Off: nothing is said, and what was waiting is dropped.
    var enabled: Bool {
        get {
            access(keyPath: \.enabled)
            return store.enabled
        }
        set {
            withMutation(keyPath: \.enabled) { store.enabled = newValue }
            if !newValue { clear() }
        }
    }

    // MARK: Where a message can show

    func setAvailable(_ place: MascotSurface, _ isAvailable: Bool) {
        if isAvailable {
            available.insert(place)
        } else {
            available.remove(place)
            if surface == place, current != nil {
                // The keyboard came up, a sheet opened, the review moved on: not now.
                dismissTask?.cancel()
                queue.interrupted(at: clock())
                hide()
            }
        }
        pump()
    }

    private var freePlace: MascotSurface? {
        if available.contains(.review) { return .review }
        if available.contains(.browser) { return .browser }
        return nil
    }

    // MARK: Saying something

    /// Queue `message`, to show `delay` seconds from now at the earliest. False when it is not
    /// said (the setting is off, or the queue drops it).
    @discardableResult
    func raise(_ message: MascotMessage, delay: TimeInterval = 0) -> Bool {
        guard enabled else { return false }
        let now = clock()
        guard queue.raise(message, at: now, notBefore: now.addingTimeInterval(delay)) else { return false }
        pump()
        return true
    }

    /// The message went: its time was up, or the reader tapped or swiped it away.
    func dismiss(_ id: UUID) {
        guard current?.id == id else { return }
        dismissTask?.cancel()
        queue.finished(id, at: clock())
        hide()
        pump()
    }

    /// Nothing more is said for now (signed out, or the setting turned off).
    func clear() {
        wakeTask?.cancel()
        dismissTask?.cancel()
        queue.clear()
        hide()
    }

    private func hide() {
        current = nil
        surface = nil
        shownAt = nil
    }

    private func pump() {
        wakeTask?.cancel()
        let now = clock()
        guard current == nil else { return }
        let place = freePlace
        if let message = queue.next(at: now, place: place), let place {
            current = message
            surface = place
            shownAt = now
            let duration = message.duration * (voiceOverRunning() ? 2 : 1)
            dismissTask = Task { [weak self] in
                do { try await Task.sleep(for: .seconds(duration)) } catch { return } // dismissed sooner
                self?.dismiss(message.id)
            }
            return
        }
        guard let wake = queue.nextWake(after: now) else { return }
        wakeTask = Task { [weak self] in
            do { try await Task.sleep(for: .seconds(wake.timeIntervalSince(now))) } catch { return } // replaced
            self?.pump()
        }
    }

    // MARK: A finished page

    /// A study page's reply was read (`ReadingProgress`): congratulate the reader if this
    /// answer finished the page, once per page for this server and account.
    func readingProgress(_ report: ReadingReport) {
        let namespace = report.namespace
        if !watcher.isLoaded(namespace) {
            do {
                watcher.load(namespace, slugs: try store.congratulated(namespace))
                unreadable.remove(namespace)
            } catch {
                unreadable.insert(namespace)
                problem = error.localizedDescription
            }
        }
        guard let done = watcher.observe(report), enabled else { return }
        do {
            try store.setCongratulated(watcher.congratulated[namespace] ?? [done.slug], namespace: namespace)
        } catch {
            problem = "Aristotle could not remember that you finished \(done.title ?? done.slug) "
                + "(\(error.localizedDescription)), so he may congratulate you on it again."
        }
        raise(MascotText.pageFinished(slug: done.slug, title: done.title))
    }

    // MARK: Today's goal

    /// Today's goal was met (`HabitCenter`, once a day). True when Aristotle says it; false when
    /// he is turned off and the plain note should show instead.
    func goalMet(answers: Int) -> Bool {
        guard enabled else { return false }
        raise(MascotText.goalMet(answers: answers))
        return true
    }

    // MARK: Welcome back

    /// The app is active now (signed in): returns when it was last active before, and keeps
    /// now. An unreadable time is said and started again from now.
    func noteActive() -> Date? {
        let before: Date?
        do {
            before = try store.lastActive()
        } catch {
            problem = error.localizedDescription + " It starts again from now."
            before = nil
        }
        store.setLastActive(clock())
        return before
    }

    /// Welcome the reader back if they were away long enough (`WelcomeBack`), once the screen
    /// has settled.
    func welcomeBack(lastActive: Date?, signedIn: Bool, snapshot: WidgetSnapshot?) {
        let now = clock()
        guard let message = WelcomeBack.message(lastActive: lastActive, now: now, signedIn: signedIn, snapshot: snapshot) else { return }
        raise(message, delay: WelcomeBack.settle)
    }

    /// Forget the memories that could not be read, so Aristotle can start them again (Settings).
    func forgetUnreadable() {
        for namespace in unreadable { store.forgetCongratulated(namespace) }
        unreadable = []
        problem = nil
    }
}

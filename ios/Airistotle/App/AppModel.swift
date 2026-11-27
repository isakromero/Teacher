import Foundation
import Observation
import WidgetKit

/// The app's state: who it is connected to, whether the server can be reached, and where the
/// page on screen came from. Owns the gateway and turns its events into state.
@MainActor
@Observable
final class AppModel {
    enum Phase: Equatable {
        /// Reading the Keychain at launch.
        case launching
        /// Not connected: the sign-in screen shows, with `notice` saying why if there is a reason.
        case signedOut(notice: String?)
        case signedIn(StoredSession)
    }

    enum Connectivity: Equatable {
        case unknown
        case online
        /// The server could not be reached the last time the app tried.
        case offline(reason: String, since: Date)
    }

    /// Where the page on screen came from.
    enum PageOrigin: Equatable {
        case none
        case server
        case savedCopy(storedAt: Date)
        /// A page saved for offline reading, answered on the phone.
        case savedPage
        case phone
    }

    private(set) var phase: Phase = .launching
    private(set) var connectivity: Connectivity = .unknown
    private(set) var pageOrigin: PageOrigin = .none
    /// A new value for each sign-in, so the web view starts fresh for each session.
    private(set) var webSession = UUID()
    /// Where the web view starts (an app target, e.g. the page that was open when the server
    /// asked to sign in again).
    private(set) var startTarget = "/"
    /// How far down to scroll `startTarget` once it has loaded (the page reopened at launch).
    private(set) var startScroll: Double?
    /// The latest graded checkpoint answer, for the haptic (a new value for each grade).
    private(set) var lastGrade: GradeFeedback?
    /// A review session to open over the page (the reader chose one, or the web review page
    /// was opened while the server cannot be reached).
    var reviewRequest: ReviewRequest?
    /// A link (widget, reminder, Shortcut, Spotlight) for the browser to carry out.
    var routedLink: RoutedLink?
    /// Why a link could not be opened (shown until dismissed).
    var linkProblem: String?
    /// A link that came before the app was signed in; it is carried out after signing in.
    var pendingLink: DeepLink?
    /// The last server typed in (not secret; prefilled on the sign-in screen).
    var lastServerText: String {
        didSet { defaults.set(lastServerText, forKey: Self.lastServerKey) }
    }

    let gateway: Gateway
    let offline: OfflineCenter
    /// Text size and keeping the screen awake.
    let reader: ReaderSettings
    /// The page each server and account was last on.
    let lastPages: LastPageStore
    /// The phone summary: widget, reminders, daily goal and streak.
    let habits: HabitCenter
    /// Aristotle's messages: a finished page, today's goal, welcome back.
    let mascot: MascotCenter
    private let auth: AuthClient
    /// The network path (Wi-Fi or not, Low Data Mode) for automatic saving.
    let network: NetworkWatcher
    private let store: any SessionStore
    private let defaults: UserDefaults
    private var pendingReturn: String?
    private var eventTask: Task<Void, Never>?

    private static let lastServerKey = "lastServer"

    init(
        transport: any HTTPTransport,
        store: any SessionStore,
        cache: ResponseCache,
        defaults: UserDefaults = .standard,
        savedRoot: URL? = nil,
        judge: any BlurbJudge = FoundationModelsJudge(),
        network: NetworkWatcher? = nil,
        sharedContainer: Result<SharedContainer, SharedContainer.Failure>? = nil,
        notifier: any ReminderNotifier = SystemReminderNotifier(),
        spotlight: any SpotlightIndexing = SystemSpotlightIndex()
    ) {
        let (events, continuation) = AsyncStream.makeStream(of: GatewayEvent.self)
        let gateway = Gateway(transport: transport, cache: cache, events: { continuation.yield($0) })
        self.gateway = gateway
        let (changes, changed) = AsyncStream.makeStream(of: Void.self, bufferingPolicy: .bufferingNewest(1))
        let root = savedRoot ?? cache.root.deletingLastPathComponent().appending(path: "SavedPages")
        let preferences = OfflinePreferences(defaults: defaults)
        let service = OfflineService(
            gateway: gateway,
            store: SavedStore(root: root),
            judge: judge,
            preferences: preferences,
            changed: { changed.yield() }
        )
        service.install()
        let review = ReviewService(gateway: gateway, store: ReviewStore(root: root), pages: service.store, grader: service.grader,
                                   changed: { changed.yield() })
        let watcher = network ?? NetworkWatcher()
        let autoSaver = AutoSaver(service: service, preferences: preferences, network: { watcher.conditions })
        self.network = watcher
        let offline = OfflineCenter(service: service, review: review, autoSaver: autoSaver, changes: changes)
        self.offline = offline
        let habitPreferences = HabitPreferences(defaults: defaults)
        self.habits = HabitCenter(
            gateway: gateway, summaryRoot: root, container: sharedContainer ?? SharedContainer.live(), notifier: notifier,
            spotlight: SpotlightBook(index: spotlight, defaults: defaults), preferences: habitPreferences,
            local: { namespace in try await Self.localHabitData(namespace, pages: service.store, review: review.store) },
            reloadWidgets: { WidgetCenter.shared.reloadAllTimelines() })
        self.auth = AuthClient(transport: transport)
        self.store = store
        self.defaults = defaults
        self.reader = ReaderSettings(defaults: defaults)
        self.lastPages = LastPageStore(defaults: defaults)
        self.mascot = MascotCenter(store: MascotStore(defaults: defaults))
        self.lastServerText = defaults.string(forKey: Self.lastServerKey) ?? ServerAddress.defaultAddress.description
        eventTask = Task { [weak self] in
            for await event in events {
                self?.handle(event)
            }
        }
        habits.isOnline = { [weak self] in
            if case .offline = self?.connectivity { false } else { true }
        }
        habits.announceGoal = { [weak self] status in self?.mascot.goalMet(answers: status.answersToday) ?? false }
        offline.onReload = { [weak self] in self?.offlineChanged() }
        offline.onSynced = { [weak self] summary in
            // What was uploaded now counts on the server: read its summary again.
            if summary.uploaded > 0 { self?.habits.refreshSoon(fetch: true) }
        }
    }

    /// The live app: URLSession, the Keychain, the cache in Application Support.
    static func live() -> AppModel {
        let root: URL
        do {
            root = try ResponseCache.defaultRoot()
        } catch {
            // Without Application Support nothing can be saved; say so rather than hide it.
            fatalError("Airistotle cannot open its Application Support folder: \(error.localizedDescription)")
        }
        let store = KeychainSessionStore()
        let cache = ResponseCache(root: root)
        #if DEBUG
        // The end-to-end UI tests (AiristotleUITests) start from a clean phone.
        if CommandLine.arguments.contains("-AiristotleResetForUITests") {
            try? store.delete()
            try? cache.clear()
            if let saved = try? SavedStore.defaultRoot() { try? FileManager.default.removeItem(at: saved) }
            UserDefaults.standard.removeObject(forKey: lastServerKey)
            for key in OfflinePreferences.allKeys { UserDefaults.standard.removeObject(forKey: key) }
            UserDefaults.standard.removeObject(forKey: TextSize.key)
            UserDefaults.standard.removeObject(forKey: ReaderSettings.keepAwakeKey)
            for key in HabitPreferences.allKeys { UserDefaults.standard.removeObject(forKey: key) }
            LastPageStore(defaults: .standard).forgetAll()
            MascotStore(defaults: .standard).forgetAll()
            if case .success(let shared) = SharedContainer.live() { try? shared.removeSnapshot() }
        }
        #endif
        return AppModel(transport: URLSessionTransport(), store: store, cache: cache)
    }

    // MARK: Launch

    /// Restore the saved session (if any), then check it with the server in the background.
    func restore() async {
        let saved: StoredSession?
        do {
            saved = try store.load()
        } catch {
            phase = .signedOut(notice: "The saved sign-in could not be read: \(error.localizedDescription) Sign in again.")
            return
        }
        guard let saved else {
            #if DEBUG
            // The UI tests and screenshots connect to a throwaway server without an account,
            // past the sign-in screen: -AiristotleConnectForUITests <address> [-AiristotleReview all|quick]
            // [-AiristotleOpen <target>].
            let arguments = CommandLine.arguments
            if let index = arguments.firstIndex(of: "-AiristotleConnectForUITests"), index + 1 < arguments.count {
                do {
                    if let open = arguments.firstIndex(of: "-AiristotleOpen"), open + 1 < arguments.count {
                        let server = try ServerAddress(parsing: arguments[index + 1])
                        lastPages.remember(target: arguments[open + 1],
                                           for: StoredSession(server: server, kind: .open).connection.cacheNamespace)
                    }
                    try await connectWithoutAccount(server: arguments[index + 1])
                    await verify(StoredSession(server: try ServerAddress(parsing: arguments[index + 1]), kind: .open))
                    welcomeBackForUITests(arguments)
                    if let review = arguments.firstIndex(of: "-AiristotleReview"), review + 1 < arguments.count {
                        let quick = arguments[review + 1] == "quick"
                        reviewRequest = ReviewRequest(kind: quick ? .quick(minutes: offline.quickReviewMinutes) : .all)
                    }
                    if let reminder = arguments.firstIndex(of: "-AiristotleReminderInSeconds"), reminder + 1 < arguments.count,
                       let seconds = Double(arguments[reminder + 1]) {
                        await habits.setReminders(true)
                        await habits.scheduleTestReminder(in: seconds)
                    }
                } catch {
                    phase = .signedOut(notice: "The UI test server could not be connected: \(error.localizedDescription)")
                }
                return
            }
            #endif
            phase = .signedOut(notice: nil)
            await habits.signedOut()
            notePendingLinkWhileSignedOut()
            return
        }
        // Reopen the page that was on screen; if it cannot be loaded now, the page or the
        // offline banner says why (it is not swapped for Home).
        let last = lastPages.page(for: saved.connection.cacheNamespace)
        connect(saved, startAt: last?.target ?? "/", scrollY: last?.scrollY)
        await verify(saved)
        // Launched after being away: welcome back (once the summary has been read).
        welcomeBack(lastActive: mascot.noteActive())
    }

    /// Ask the server whether the session still holds. Unreachable: keep it (saved pages can
    /// still be read), and the offline banner says why nothing loads fresh.
    func verify(_ session: StoredSession) async {
        do {
            if let token = session.token {
                _ = try await auth.me(server: session.server, token: token)
            } else if try await auth.mode(of: session.server) == .accounts {
                endSession(notice: "\(session.server.host) now requires an account. Sign in to continue.")
                return
            }
            connectivity = .online
            await offline.syncNow()
            await offline.refreshReview()
            await habits.refresh(fetch: true)
            await offline.autoSave(current: currentStudyPage)
        } catch .server(status: 401, _) {
            endSession(notice: "Your session on \(session.server.host) has ended. Sign in again.")
        } catch .unreachable(let reason) {
            connectivity = .offline(reason: reason, since: Date())
            // The widget's count comes from the questions saved on the phone.
            await habits.refresh(fetch: false)
        } catch .accountsOff {
            endSession(notice: "\(session.server.host) no longer has accounts. Connect without an account.")
        } catch {
            // Any other answer: keep the session; the page will show the server's own error.
            connectivity = .online
        }
    }

    // MARK: Signing in

    func signIn(server text: String, email: String, password: String) async throws {
        let server = try ServerAddress(parsing: text)
        try Self.requireFilled(email: email, password: password)
        let new = try await auth.login(server: server, email: email, password: password)
        try finishSignIn(server: server, new)
    }

    func register(server text: String, email: String, password: String, accessCode: String) async throws {
        let server = try ServerAddress(parsing: text)
        try Self.requireFilled(email: email, password: password)
        guard !accessCode.isEmpty else { throw AuthError.unexpected("Enter the access code the server's owner gave you.") }
        let new = try await auth.register(server: server, email: email, password: password, accessCode: accessCode)
        try finishSignIn(server: server, new)
    }

    /// Connect to a server that runs without accounts; refused (with the reason) when the
    /// server has accounts.
    func connectWithoutAccount(server text: String) async throws {
        let server = try ServerAddress(parsing: text)
        guard try await auth.mode(of: server) == .open else { throw AuthError.accountsRequired }
        let session = StoredSession(server: server, kind: .open)
        try store.save(session)
        lastServerText = server.description
        connectivity = .online
        mascot.store.setLastActive(Date()) // no welcome right after signing in
        connectAfterSignIn(session)
    }

    private func finishSignIn(server: ServerAddress, _ new: NewSession) throws {
        let session = StoredSession(server: server, kind: .account(token: new.token, user: new.user, expiresAt: new.expiresAt))
        try store.save(session)
        lastServerText = server.description
        connectivity = .online
        mascot.store.setLastActive(Date()) // no welcome right after signing in
        connectAfterSignIn(session)
    }

    /// After signing in: the page the server interrupted (a 401) if there is one, else the
    /// page this account was last on here, else Home.
    private func connectAfterSignIn(_ session: StoredSession) {
        let last = lastPages.page(for: session.connection.cacheNamespace)
        if let pendingReturn {
            connect(session, startAt: pendingReturn, scrollY: last?.target == pendingReturn ? last?.scrollY : nil)
        } else {
            connect(session, startAt: last?.target ?? "/", scrollY: last?.scrollY)
        }
    }

    private static func requireFilled(email: String, password: String) throws {
        if email.trimmingCharacters(in: .whitespaces).isEmpty || password.isEmpty {
            throw AuthError.unexpected("Enter your email address and your password.")
        }
    }

    private func connect(_ session: StoredSession, startAt target: String, scrollY: Double? = nil) {
        gateway.connection = session.connection
        pendingReturn = nil
        startTarget = target
        startScroll = scrollY
        pageOrigin = .none
        webSession = UUID()
        phase = .signedIn(session)
        if let link = pendingLink {
            pendingLink = nil
            open(link)
        }
        Task { await offline.reload() }
    }

    /// The app came to the foreground: upload what waits, if the server can be reached.
    func becameActive() {
        guard case .signedIn = phase else { return }
        let lastActive = mascot.noteActive()
        Task {
            await habits.refreshAuthorization()
            await offline.syncNow()
            await offline.refreshReview()
            await habits.refresh(fetch: true)
            // Back after a while: welcome back, with what is due as just read.
            welcomeBack(lastActive: lastActive)
            await offline.autoSave(current: currentStudyPage)
        }
    }

    /// The app went to the background: ask iOS for a background refresh of the widget's data.
    func wentToBackground() {
        guard case .signedIn = phase else { return }
        _ = mascot.noteActive() // the time away counts from now
        habits.scheduleBackgroundRefresh()
    }

    /// iOS woke the app to refresh (`BGAppRefreshTask`): read the summary and pass it on. The
    /// session is read from the Keychain if the app was launched for this alone.
    func backgroundRefresh() async {
        if case .launching = phase {
            guard let saved = try? store.load() else { return }
            gateway.connection = saved.connection
        }
        guard gateway.connection != nil else { return }
        await offline.syncNow()
        await offline.refreshReview()
        await habits.refresh(fetch: true)
        habits.scheduleBackgroundRefresh()
    }

    /// The study page on screen (from the last page shown), for automatic saving.
    private(set) var currentStudyPage: String?

    /// A review session finished: fetch the review queue again for offline use.
    func reviewSessionEnded() {
        Task {
            await offline.refreshReview()
            await habits.refresh(fetch: true)
        }
    }

    // MARK: The page on screen

    /// The web view shows `target`: remember it for the next launch (this server and account).
    func rememberPage(_ target: String) {
        guard case .signedIn = phase, let namespace = gateway.connection?.cacheNamespace else { return }
        lastPages.remember(target: target, for: namespace)
    }

    /// The page `target` is scrolled `scrollY` CSS pixels down.
    func rememberScroll(_ target: String, scrollY: Double) {
        guard case .signedIn = phase, let namespace = gateway.connection?.cacheNamespace else { return }
        lastPages.remember(scrollY: scrollY, of: target, for: namespace)
    }

    // MARK: Signing out

    /// Sign out: end the session on the server, then forget it on the phone. Returns a warning
    /// when the server could not be told (the session then stays valid there until it expires).
    @discardableResult
    func signOut() async -> String? {
        guard case .signedIn(let session) = phase else { return nil }
        gateway.connection = nil
        // Signing out forgets where this account was: the next sign-in starts at Home.
        lastPages.forget(session.connection.cacheNamespace)
        await offline.forgetReview(namespace: session.connection.cacheNamespace)
        var notice: String?
        if let token = session.token {
            do {
                try await auth.logout(server: session.server, token: token)
            } catch {
                notice = "Signed out on this phone, but \(session.server.host) could not be told "
                    + "(\(error.localizedDescription)). That session stays valid on the server until it expires."
            }
        }
        do {
            try store.delete()
        } catch {
            notice = [notice, "The saved sign-in could not be removed from the Keychain: \(error.localizedDescription)"]
                .compactMap(\.self).joined(separator: " ")
        }
        pendingReturn = nil
        phase = .signedOut(notice: notice)
        mascot.clear()
        await habits.signedOut()
        await offline.reload()
        return notice
    }

    /// The server refused the session: forget it and show the sign-in screen.
    private func endSession(notice: String, returnTo: String? = nil) {
        guard case .signedIn = phase else { return }
        gateway.connection = nil
        var message = notice
        do {
            try store.delete()
        } catch {
            message += " (The saved sign-in could not be removed from the Keychain: \(error.localizedDescription))"
        }
        pendingReturn = returnTo
        phase = .signedOut(notice: message)
        mascot.clear()
        Task { await habits.signedOut() }
    }

    // MARK: Gateway events

    func handle(_ event: GatewayEvent) {
        switch event {
        case .serverReachable:
            let wasOffline = if case .offline = connectivity { true } else { false }
            connectivity = .online
            // Back online: upload what was answered meanwhile.
            if wasOffline, offline.hasPendingWork { Task { await offline.syncNow() } }
        case .serverUnreachable(let reason):
            if case .offline(_, let since) = connectivity {
                connectivity = .offline(reason: reason, since: since)
            } else {
                connectivity = .offline(reason: reason, since: Date())
            }
        case .sessionRejected(let returnTo):
            guard case .signedIn(let session) = phase else { return }
            let notice = session.token == nil
                ? "\(session.server.host) now requires an account. Sign in to continue."
                : "Your session on \(session.server.host) has ended. Sign in again."
            endSession(notice: notice, returnTo: returnTo)
        case .signOutRequested:
            Task { await signOut() }
        case .pageShown(let target, let source):
            pageShown(target, source: source)
            switch source {
            case .server: pageOrigin = .server
            case .cache(let storedAt): pageOrigin = .savedCopy(storedAt: storedAt)
            case .local(ReadingEngine.sourceName): pageOrigin = .savedPage
            case .local, .gatewayError: pageOrigin = .phone
            }
        case .answerGraded(let outcome):
            lastGrade = GradeFeedback(outcome: outcome)
            // The answer counts towards today's goal: read the summary again shortly.
            habits.refreshSoon(fetch: true)
        case .readingProgress(let report):
            mascot.readingProgress(report)
        }
    }

    /// What follows from the page on screen: a study page from the server may be saved
    /// automatically; the web review page shown without the server (a saved copy that could
    /// not send an answer) gives way to the native review of the questions saved on the phone.
    private func pageShown(_ target: String, source: ReplySource) {
        let path = String(target.prefix { $0 != "?" && $0 != "#" })
        let slug = OfflineService.pageSlug(path)
        if slug != nil || source == .server { currentStudyPage = slug }
        if let slug, source == .server {
            Task { await offline.autoSave(current: slug) }
        }
        let offlineCopy = switch source {
        case .cache, .gatewayError: true
        case .server, .local: false
        }
        if path == ReviewRequest.webPath, offlineCopy {
            let concept = URLComponents(string: "x://y\(target)")?.queryItems?.first { $0.name == "concept" }?.value
            reviewRequest = ReviewRequest(kind: .all, concept: concept, reason: .webPageOffline)
        }
    }
}

extension AppModel {
    struct GradeFeedback: Equatable {
        let id = UUID()
        let outcome: AnswerOutcome
    }

    // MARK: Saved copies

    func cacheUsage() async -> (bytes: Int64, entries: Int) {
        let cache = gateway.cache
        return await Task.detached { (cache.usage(), cache.entryCount()) }.value
    }

    func clearCache() async throws {
        let cache = gateway.cache
        try await Task.detached { try cache.clear() }.value
    }
}

import Foundation
import Observation

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
    private let auth: AuthClient
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
        judge: any BlurbJudge = FoundationModelsJudge()
    ) {
        let (events, continuation) = AsyncStream.makeStream(of: GatewayEvent.self)
        let gateway = Gateway(transport: transport, cache: cache, events: { continuation.yield($0) })
        self.gateway = gateway
        let (changes, changed) = AsyncStream.makeStream(of: Void.self, bufferingPolicy: .bufferingNewest(1))
        let service = OfflineService(
            gateway: gateway,
            store: SavedStore(root: savedRoot ?? cache.root.deletingLastPathComponent().appending(path: "SavedPages")),
            judge: judge,
            preferences: OfflinePreferences(defaults: defaults),
            changed: { changed.yield() }
        )
        service.install()
        self.offline = OfflineCenter(service: service, changes: changes)
        self.auth = AuthClient(transport: transport)
        self.store = store
        self.defaults = defaults
        self.reader = ReaderSettings(defaults: defaults)
        self.lastPages = LastPageStore(defaults: defaults)
        self.lastServerText = defaults.string(forKey: Self.lastServerKey) ?? ServerAddress.defaultAddress.description
        eventTask = Task { [weak self] in
            for await event in events {
                self?.handle(event)
            }
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
            UserDefaults.standard.removeObject(forKey: OfflinePreferences.gradingKey)
            UserDefaults.standard.removeObject(forKey: TextSize.key)
            UserDefaults.standard.removeObject(forKey: ReaderSettings.keepAwakeKey)
            LastPageStore(defaults: .standard).forgetAll()
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
            phase = .signedOut(notice: nil)
            return
        }
        // Reopen the page that was on screen; if it cannot be loaded now, the page or the
        // offline banner says why (it is not swapped for Home).
        let last = lastPages.page(for: saved.connection.cacheNamespace)
        connect(saved, startAt: last?.target ?? "/", scrollY: last?.scrollY)
        await verify(saved)
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
        } catch .server(status: 401, _) {
            endSession(notice: "Your session on \(session.server.host) has ended. Sign in again.")
        } catch .unreachable(let reason) {
            connectivity = .offline(reason: reason, since: Date())
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
        connectAfterSignIn(session)
    }

    private func finishSignIn(server: ServerAddress, _ new: NewSession) throws {
        let session = StoredSession(server: server, kind: .account(token: new.token, user: new.user, expiresAt: new.expiresAt))
        try store.save(session)
        lastServerText = server.description
        connectivity = .online
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
        Task { await offline.reload() }
    }

    /// The app came to the foreground: upload what waits, if the server can be reached.
    func becameActive() {
        guard case .signedIn = phase else { return }
        Task { await offline.syncNow() }
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
    }

    // MARK: Gateway events

    func handle(_ event: GatewayEvent) {
        switch event {
        case .serverReachable:
            let wasOffline = if case .offline = connectivity { true } else { false }
            connectivity = .online
            // Back online: upload what was answered meanwhile.
            if wasOffline, offline.pendingCount > 0 { Task { await offline.syncNow() } }
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
        case .pageShown(_, let source):
            switch source {
            case .server: pageOrigin = .server
            case .cache(let storedAt): pageOrigin = .savedCopy(storedAt: storedAt)
            case .local(ReadingEngine.sourceName): pageOrigin = .savedPage
            case .local, .gatewayError: pageOrigin = .phone
            }
        case .answerGraded(let outcome):
            lastGrade = GradeFeedback(outcome: outcome)
        }
    }

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

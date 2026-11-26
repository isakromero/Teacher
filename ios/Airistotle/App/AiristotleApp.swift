import SwiftUI

@main
struct AiristotleApp: App {
    @State private var model: AppModel
    @Environment(\.scenePhase) private var scenePhase

    init() {
        if ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil {
            // Under unit tests the app is only the tests' host: no Keychain, no network.
            _model = State(initialValue: AppModel(
                transport: UnreachableTransport(), store: EphemeralSessionStore(),
                cache: ResponseCache(root: FileManager.default.temporaryDirectory.appending(path: "AiristotleHostCache"))
            ))
        } else {
            _model = State(initialValue: AppModel.live())
        }
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .task { await model.restore() }
                .onChange(of: scenePhase) { _, phase in
                    if phase == .active { model.becameActive() }
                }
                .onChange(of: model.phase) { _, phase in
                    // Only a study page on screen keeps the screen on (BrowserView); signed out, never.
                    if case .signedIn = phase { return }
                    UIApplication.shared.isIdleTimerDisabled = false
                }
        }
    }
}

/// Signed in or not.
struct RootView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        switch model.phase {
        case .launching:
            // The launch screen's colour, which is the page's.
            ProgressView()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color.pageBackground)
        case .signedOut(let notice):
            SignInView(notice: notice)
        case .signedIn(let session):
            BrowserView(session: session)
                .id(model.webSession) // a fresh web view for each session
        }
    }
}

/// The transport of the tests' host app: every request fails as unreachable.
struct UnreachableTransport: HTTPTransport {
    func send(_ request: URLRequest, followRedirects: Bool) async throws -> TransportResponse {
        throw URLError(.notConnectedToInternet)
    }
}

/// A session store in memory (the tests' host app).
final class EphemeralSessionStore: SessionStore, @unchecked Sendable {
    private let lock = NSLock()
    private var session: StoredSession?

    func load() throws -> StoredSession? { lock.withLock { session } }
    func save(_ session: StoredSession) throws { lock.withLock { self.session = session } }
    func delete() throws { lock.withLock { session = nil } }
}

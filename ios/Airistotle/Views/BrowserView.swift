import SwiftUI

/// The main screen: the Teacher web client, an offline banner, and a bottom bar (Save for
/// offline, Saved, a menu with Reload, Settings) that slides away while the reader scrolls
/// down. Back and forward are the edge swipes, reload is also pull to refresh.
struct BrowserView: View {
    let session: StoredSession

    @Environment(AppModel.self) private var model
    @Environment(\.openURL) private var openURL
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityVoiceOverEnabled) private var voiceOver
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var host: WebViewHost?
    @State private var showingSettings = false
    @State private var showingSaved = false
    @State private var showingProgress = false
    @State private var saveProblem: String?
    @State private var barHeight: CGFloat = 0
    @State private var bottomSafeArea: CGFloat = 0

    /// The space between the bar and the bottom of the safe area.
    private static let barGap: CGFloat = 6

    var body: some View {
        VStack(spacing: 0) {
            StatusBanner(connectivity: model.connectivity, pageOrigin: model.pageOrigin, server: session.server,
                         pending: model.offline.pendingCount) {
                host?.reload()
            }
            ZStack(alignment: .top) {
                if let host {
                    // Down to the bottom of the screen, under the bar: the page shows there when
                    // the bar is away, and keeps that space free for its own controls (reserveBottom).
                    WebView(host: host)
                        .ignoresSafeArea(.container, edges: .bottom)
                    if host.isLoading {
                        ProgressView(value: max(host.progress, 0.05))
                            .progressViewStyle(.linear)
                            .tint(.accentColor)
                            .accessibilityLabel("Loading")
                    }
                }
            }
        }
        .overlay { barLayer }
        .overlay { mascotLayer }
        // The status bar area and the space around the page in the page's own colours.
        .background(Color.pageSurface.ignoresSafeArea())
        .sheet(isPresented: $showingSettings) {
            SettingsView(session: session)
        }
        .sheet(isPresented: $showingSaved) {
            SavedPagesView { slug in host?.load(target: "/artifacts/\(StudyPageRequests.segment(slug))") }
        }
        .sheet(isPresented: $showingProgress) {
            StudyProgressView()
        }
        .fullScreenCover(item: reviewBinding, onDismiss: { model.reviewSessionEnded() }, content: { request in
            ReviewSessionView(
                session: ReviewSession(request: request, review: model.offline.review, offline: isOffline),
                assets: RendererAssets(review: model.offline.review),
                server: session.server.host,
                habits: model.habits,
                mascot: model.mascot,
                close: { model.reviewRequest = nil }
            )
            .goalAcknowledgement(model.habits)
        })
        .goalAcknowledgement(model.habits)
        .onChange(of: model.routedLink) { _, link in
            if let link { carryOut(link) }
        }
        .alert("Not saved", isPresented: Binding(get: { saveProblem != nil }, set: { if !$0 { saveProblem = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(saveProblem ?? "")
        }
        .sheet(item: sharedFileBinding) { file in
            ShareSheet(items: [file.url])
        }
        .alert(
            host?.problem?.title ?? "",
            isPresented: problemBinding,
            presenting: host?.problem
        ) { problem in
            if problem.offersReload {
                Button("Reload") { host?.reload() }
                    .accessibilityIdentifier("problem.reload")
            }
            Button("OK", role: .cancel) {}
        } message: { problem in
            Text(problem.message)
        }
        .sensoryFeedback(trigger: model.lastGrade) { _, grade in
            grade.map { ReviewSessionView.haptic($0.outcome) }
        }
        .onChange(of: host?.target) { _, target in
            if let target { model.rememberPage(target) }
        }
        .onChange(of: model.reader.textSizePercent) { _, percent in
            host?.setTextSize(percent)
        }
        .onChange(of: keepAwake, initial: true) { _, awake in
            UIApplication.shared.isIdleTimerDisabled = awake
        }
        .onChange(of: barHeight + bottomSafeArea) { _, _ in reserveBottom() }
        .onChange(of: mascotFree, initial: true) { _, free in model.mascot.setAvailable(.browser, free) }
        .onAppear(perform: start)
        .onDisappear {
            // Signed out (or a new session's web view): the screen may sleep again.
            UIApplication.shared.isIdleTimerDisabled = false
            model.mascot.setAvailable(.browser, false)
        }
    }

    // MARK: Aristotle

    /// Aristotle may speak over the page: nothing covers it, the keyboard is down, the app is
    /// in front and the page has finished loading.
    private var mascotFree: Bool {
        guard let host, scenePhase == .active else { return false }
        return !host.keyboardShown && !host.isLoading && host.problem == nil && host.sharedFile == nil
            && !showingSettings && !showingSaved && !showingProgress && model.reviewRequest == nil
            && saveProblem == nil && model.linkProblem == nil
    }

    /// Above the bar and, on a study page or lab, above the page's Tutor button (`.tutor-fab`:
    /// 1rem from the bottom, about 3rem tall, scaled with the text size).
    private var mascotLayer: some View {
        let path = String((host?.target ?? "").prefix { $0 != "?" && $0 != "#" })
        let tutor = path.hasPrefix("/artifacts/") || path.hasPrefix("/labs/")
            ? 4 * 16 * CGFloat(model.reader.textSizePercent) / 100 : 0
        return MascotOverlay(center: model.mascot, place: .browser, bottomInset: barHeight + Self.barGap + tutor + 8) { action in
            switch action {
            case .startReview: model.reviewRequest = ReviewRequest(kind: .all)
            case .continueStudying: model.open(.continueStudying)
            }
        }
        .ignoresSafeArea(.keyboard)
    }

    // MARK: The bottom bar

    /// The bar shows while scrolling up, near the top and the end of a page, on every new
    /// page, and always with VoiceOver (whose scrolling would otherwise hide it for good).
    private var barShown: Bool { voiceOver || (host?.barVisible ?? true) }

    private var barLayer: some View {
        GeometryReader { proxy in
            VStack {
                Spacer()
                bar
                    .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { barHeight = $0 }
                    .padding(.bottom, Self.barGap)
                    .offset(y: barShown ? 0 : barHeight + Self.barGap + proxy.safeAreaInsets.bottom + 12)
                    .opacity(barShown ? 1 : 0)
                    .animation(reduceMotion ? .easeInOut(duration: 0.15) : .snappy(duration: 0.3), value: barShown)
                    .allowsHitTesting(barShown)
                    .accessibilityHidden(!barShown)
            }
            .frame(maxWidth: .infinity)
            .onChange(of: proxy.safeAreaInsets.bottom, initial: true) { _, inset in bottomSafeArea = inset }
        }
        // Behind the keyboard, not on top of it.
        .ignoresSafeArea(.keyboard)
    }

    private var bar: some View {
        HStack(spacing: 4) {
            saveButton
            Button { showingSaved = true } label: {
                barIcon("tray.full")
                    .overlay(alignment: .topTrailing) { pendingBadge }
            }
            .accessibilityLabel("Saved")
            .accessibilityValue(pendingText)
            .accessibilityIdentifier("browser.saved")
            reviewMenu
            Menu {
                Button("Reload", systemImage: "arrow.clockwise") { host?.reload() }
                    .accessibilityIdentifier("browser.reload")
                Button("Find on Page", systemImage: "magnifyingglass") { host?.findOnPage() }
                    .accessibilityIdentifier("browser.find")
            } label: {
                barIcon("ellipsis")
            }
            .accessibilityLabel("More")
            .accessibilityIdentifier("browser.more")
            Button { showingSettings = true } label: { barIcon("gearshape") }
                .accessibilityLabel("Settings")
                .accessibilityIdentifier("browser.settings")
        }
        .padding(.horizontal, 6)
        .padding(.vertical, 4)
        .glassEffect(.regular.interactive(), in: .capsule)
    }

    // MARK: Review

    /// The review entry point: everything due, a quick time-boxed review, or the web page.
    private var reviewMenu: some View {
        let due = model.offline.reviewDue()
        let minutes = model.offline.quickReviewMinutes
        return Menu {
            Button(due > 0 ? "Review now (\(due) due)" : "Review now", systemImage: "rectangle.stack") {
                model.reviewRequest = ReviewRequest(kind: .all)
            }
            .accessibilityIdentifier("browser.reviewAll")
            Button("Quick review (\(minutes) min)", systemImage: "timer") {
                model.reviewRequest = ReviewRequest(kind: .quick(minutes: minutes))
            }
            .accessibilityIdentifier("browser.reviewQuick")
            Button("Open the review page", systemImage: "safari") { host?.load(target: ReviewRequest.webPath) }
                .accessibilityIdentifier("browser.reviewWeb")
            Divider()
            Button(HabitText.menuLine(model.habits.goalStatus), systemImage: "chart.bar") { showingProgress = true }
                .accessibilityIdentifier("browser.progress")
        } label: {
            barIcon("rectangle.stack")
                .overlay(alignment: .topTrailing) { badge(due, color: .pageAccent, identifier: "browser.reviewBadge") }
        }
        .accessibilityLabel("Review")
        .accessibilityValue(due == 0 ? "" : "\(due) due")
        .accessibilityIdentifier("browser.review")
    }

    private var reviewBinding: Binding<ReviewRequest?> {
        Binding(get: { model.reviewRequest }, set: { model.reviewRequest = $0 })
    }

    private var isOffline: Bool {
        if case .offline = model.connectivity { true } else { false }
    }

    @ViewBuilder private func badge(_ count: Int, color: Color, identifier: String) -> some View {
        if count > 0 {
            Text(count > 99 ? "99+" : "\(count)")
                .font(.caption2.weight(.bold).monospacedDigit())
                .foregroundStyle(Color.pageAccentForeground)
                .padding(.horizontal, 5)
                .frame(minWidth: 18, minHeight: 18)
                .background(Capsule().fill(color))
                .offset(x: -2, y: 2)
                .accessibilityHidden(true)
                .accessibilityIdentifier(identifier)
        }
    }

    private func barIcon(_ systemImage: String) -> some View {
        Image(systemName: systemImage)
            .font(.system(size: 19, weight: .medium))
            .frame(width: 48, height: 44)
            .contentShape(.rect)
    }

    /// Answers made offline that wait to upload.
    @ViewBuilder private var pendingBadge: some View {
        let pending = model.offline.pendingCount
        if pending > 0 {
            Text(pending > 99 ? "99+" : "\(pending)")
                .font(.caption2.weight(.bold).monospacedDigit())
                .foregroundStyle(.white)
                .padding(.horizontal, 5)
                .frame(minWidth: 18, minHeight: 18)
                .background(Capsule().fill(Color.red))
                .offset(x: -2, y: 2)
                .accessibilityHidden(true)
                .accessibilityIdentifier("browser.pendingBadge")
        }
    }

    private var pendingText: String {
        switch model.offline.pendingCount {
        case 0: ""
        case 1: "1 answer waiting to upload"
        case let count: "\(count) answers waiting to upload"
        }
    }

    private func reserveBottom() {
        host?.reserveBottom(barHeight + Self.barGap + bottomSafeArea)
    }

    /// The study page on screen, if it is one (labs have no offline bundle).
    private var currentSlug: String? {
        guard let target = host?.target else { return nil }
        return OfflineService.pageSlug(String(target.prefix { $0 != "?" }))
    }

    /// "Save for offline" on a study page; saved: a menu to refresh it or see the saved pages.
    @ViewBuilder private var saveButton: some View {
        if let slug = currentSlug {
            if model.offline.saving[slug] != nil {
                ProgressView()
                    .frame(width: 48, height: 44)
                    .accessibilityLabel("Saving for offline")
                    .accessibilityIdentifier("browser.saving")
            } else if let saved = model.offline.summary(slug) {
                Menu {
                    Text("Saved \(saved.savedAt.formatted(date: .abbreviated, time: .shortened))")
                    Button("Refresh saved copy", systemImage: "arrow.clockwise") { save(slug) }
                    Button("Saved pages", systemImage: "tray.full") { showingSaved = true }
                } label: {
                    barIcon(saved.regenerated ? "exclamationmark.arrow.circlepath" : "checkmark.circle.fill")
                }
                .accessibilityLabel("Saved for offline")
                .accessibilityIdentifier("browser.savedState")
            } else {
                Button { save(slug) } label: { barIcon("arrow.down.circle") }
                    .accessibilityLabel("Save for offline")
                    .accessibilityIdentifier("browser.save")
            }
        }
    }

    private func save(_ slug: String) {
        Task {
            do {
                try await model.offline.save(slug)
            } catch {
                saveProblem = error.localizedDescription
            }
        }
    }

    // MARK: Screen

    private var keepAwake: Bool {
        ScreenAwakePolicy.keepAwake(
            enabled: model.reader.keepScreenAwake, target: host?.target, appActive: scenePhase == .active, signedIn: true
        )
    }

    private func start() {
        guard host == nil else { return }
        let model = model
        let host = WebViewHost(
            gateway: model.gateway, server: session.server, textSizePercent: model.reader.textSizePercent,
            rememberScroll: { target, scrollY in model.rememberScroll(target, scrollY: scrollY) },
            openExternally: { url in openURL(url) }
        )
        self.host = host
        reserveBottom()
        if let link = model.routedLink, case .load(let target) = link.action {
            // Opened from a link: its page, not the one last open.
            model.routedLink = nil
            host.load(target: target)
        } else {
            host.load(target: model.startTarget, scrollY: model.startScroll)
            if let link = model.routedLink { carryOut(link) }
        }
    }

    // MARK: Links

    /// A link from the widget, a reminder, a Shortcut or Spotlight: close what covers the page,
    /// then open the review or load the page.
    private func carryOut(_ link: RoutedLink) {
        model.routedLink = nil
        let covered = showingSettings || showingSaved || showingProgress
        showingSettings = false
        showingSaved = false
        showingProgress = false
        Task {
            // A sheet that is closing cannot be replaced at once.
            if covered { try? await Task.sleep(for: .milliseconds(700)) }
            switch link.action {
            case .review(let kind):
                model.reviewRequest = ReviewRequest(kind: kind)
            case .load(let target):
                if model.reviewRequest != nil {
                    model.reviewRequest = nil
                    try? await Task.sleep(for: .milliseconds(700))
                }
                host?.load(target: target)
            }
        }
    }

    private var problemBinding: Binding<Bool> {
        Binding(get: { host?.problem != nil }, set: { if !$0 { host?.problem = nil } })
    }

    private var sharedFileBinding: Binding<SharedFile?> {
        Binding(
            get: { host?.sharedFile.map(SharedFile.init) },
            set: { if $0 == nil { host?.sharedFile = nil } }
        )
    }
}

private struct SharedFile: Identifiable {
    let url: URL
    var id: URL { url }
}

/// Says when the server cannot be reached, and when the page on screen is a saved copy.
struct StatusBanner: View {
    let connectivity: AppModel.Connectivity
    let pageOrigin: AppModel.PageOrigin
    let server: ServerAddress
    let pending: Int
    let retry: () -> Void

    var body: some View {
        if let message {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Image(systemName: "wifi.slash")
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text(message.title).font(.subheadline.weight(.semibold))
                    Text(message.detail).font(.footnote)
                }
                Spacer(minLength: 8)
                Button("Retry", action: retry)
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .accessibilityIdentifier("banner.retry")
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .foregroundStyle(.white)
            .background(Color.orange.gradient)
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("banner.offline")
        }
    }

    private var message: (title: String, detail: String)? {
        guard case .offline = connectivity else { return nil }
        let waiting = pending == 0 ? "" : " \(pending) answer(s) wait to sync."
        switch pageOrigin {
        case .savedPage:
            return (
                "Offline: your saved page",
                "Checkpoints are graded on this phone and uploaded when \(server.host) can be reached.\(waiting)"
            )
        case .savedCopy(let storedAt):
            return (
                "Offline: showing a saved copy",
                "Saved \(storedAt.formatted(.relative(presentation: .named))). \(server.host) cannot be reached; "
                    + "nothing you do here is sent."
            )
        default:
            return (
                "Offline",
                "\(server.host) cannot be reached. Pages saved on this phone still open; nothing else you do is sent.\(waiting)"
            )
        }
    }
}

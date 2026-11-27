import Observation
import UIKit
import WebKit

/// Owns the web view that shows the Teacher web client, unchanged, from `teacher://app/`, and
/// decides what happens with links, new windows, downloads and failures.
@MainActor
@Observable
final class WebViewHost: NSObject {
    struct Problem: Identifiable, Equatable {
        let id = UUID()
        var title: String
        var message: String
        /// A page that failed to load: the alert offers to load it again.
        var offersReload = false
    }

    private(set) var canGoBack = false
    private(set) var canGoForward = false
    private(set) var isLoading = false
    private(set) var progress = 0.0
    private(set) var title = ""
    /// The app target of the page on screen (`/library`).
    private(set) var target: String?
    /// A file the page downloaded, to hand to the share sheet.
    var sharedFile: URL?
    /// A failure to show (a page that could not load, a download that failed).
    var problem: Problem?
    /// Whether the bottom bar shows (it hides while the reader scrolls down, `BarVisibility`).
    private(set) var barVisible = true
    /// Whether the keyboard is up (the page's text field has it).
    private(set) var keyboardShown = false

    @ObservationIgnored let webView: WKWebView
    @ObservationIgnored private let server: ServerAddress
    @ObservationIgnored private let gateway: Gateway
    @ObservationIgnored private let schemeHandler: SchemeHandler
    @ObservationIgnored private var observations: [NSKeyValueObservation] = []
    @ObservationIgnored private let refreshControl = UIRefreshControl()
    @ObservationIgnored private var downloads: [ObjectIdentifier: URL] = [:]
    @ObservationIgnored private let openExternally: @MainActor (URL) -> Void
    @ObservationIgnored private let rememberScroll: @MainActor (_ target: String, _ scrollY: Double) -> Void
    @ObservationIgnored private var bar = BarVisibility()
    @ObservationIgnored private var textSizePercent: Int
    /// The scroll position to go back to once `target` has loaded (the page reopened at launch).
    @ObservationIgnored private var pendingScroll: (target: String, scrollY: Double)?
    /// The height at the bottom of the web view the bottom bar may cover (`reserveBottom`).
    @ObservationIgnored private var bottomReserve: CGFloat = 0

    init(
        gateway: Gateway,
        server: ServerAddress,
        textSizePercent: Int = TextSize.defaultPercent,
        rememberScroll: @escaping @MainActor (_ target: String, _ scrollY: Double) -> Void = { _, _ in },
        openExternally: @escaping @MainActor (URL) -> Void
    ) {
        self.server = server
        self.gateway = gateway
        self.openExternally = openExternally
        self.rememberScroll = rememberScroll
        self.textSizePercent = textSizePercent
        schemeHandler = SchemeHandler(gateway: gateway)
        let configuration = WKWebViewConfiguration()
        configuration.setURLSchemeHandler(schemeHandler, forURLScheme: AppURL.scheme)
        configuration.websiteDataStore = .default() // localStorage persists (chat ids, widths)
        configuration.allowsInlineMediaPlayback = true
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        configuration.dataDetectorTypes = []
        PageShims.install(in: configuration.userContentController, textSizePercent: textSizePercent)
        let messages = ScriptMessageProxy()
        configuration.userContentController.add(messages, name: PageShims.scrollHandler)
        webView = WKWebView(frame: .zero, configuration: configuration)
        super.init()
        messages.host = self
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.isFindInteractionEnabled = true
        // A long press on a link would preview the app's internal teacher:// address.
        webView.allowsLinkPreview = false
        // The page's own colours until it paints, and past its ends: never a white flash or strip.
        webView.isOpaque = false
        webView.backgroundColor = PageColors.background
        webView.scrollView.backgroundColor = PageColors.background
        webView.underPageBackgroundColor = PageColors.surface
        #if DEBUG
        webView.isInspectable = true // Safari's Web Inspector, in development builds only
        #endif
        refreshControl.addTarget(self, action: #selector(pulledToRefresh), for: .valueChanged)
        webView.scrollView.refreshControl = refreshControl
        observe()
        observeKeyboard()
    }

    private func observe() {
        observations = [
            webView.observe(\.canGoBack, options: [.initial, .new]) { [weak self] view, _ in
                MainActor.assumeIsolated { self?.canGoBack = view.canGoBack }
            },
            webView.observe(\.canGoForward, options: [.initial, .new]) { [weak self] view, _ in
                MainActor.assumeIsolated { self?.canGoForward = view.canGoForward }
            },
            webView.observe(\.isLoading, options: [.initial, .new]) { [weak self] view, _ in
                MainActor.assumeIsolated { self?.isLoading = view.isLoading }
            },
            webView.observe(\.estimatedProgress, options: [.initial, .new]) { [weak self] view, _ in
                MainActor.assumeIsolated { self?.progress = view.estimatedProgress }
            },
            webView.observe(\.title, options: [.initial, .new]) { [weak self] view, _ in
                MainActor.assumeIsolated { self?.title = Self.shortTitle(view.title) }
            },
            webView.observe(\.url, options: [.initial, .new]) { [weak self] view, _ in
                MainActor.assumeIsolated { self?.target = view.url.flatMap(AppURL.target(of:)) }
            },
            webView.scrollView.observe(\.contentOffset, options: [.new]) { [weak self] scrollView, _ in
                MainActor.assumeIsolated { self?.scrolled(scrollView) }
            }
        ]
    }

    // MARK: Scrolling

    private func scrolled(_ scrollView: UIScrollView) {
        let inset = scrollView.adjustedContentInset
        let offset = scrollView.contentOffset.y + inset.top
        bar.update(BarVisibility.Sample(
            offset: offset,
            contentHeight: scrollView.contentSize.height,
            viewportHeight: scrollView.bounds.height - inset.top - inset.bottom,
            byReader: scrollView.isTracking || scrollView.isDragging || scrollView.isDecelerating
        ))
        if bar.isVisible != barVisible { barVisible = bar.isVisible }
        // Past the top the header's colour shows, past the end the page's.
        let under = offset < scrollView.bounds.height / 2 ? PageColors.surface : PageColors.background
        if webView.underPageBackgroundColor != under { webView.underPageBackgroundColor = under }
    }

    /// Keep the bottom `height` points of the web view for the bottom bar, for good: the page's
    /// fixed controls (the tutor button, the tutor's composer) sit above it and its last line
    /// scrolls clear of it, whether the bar shows or not, so hiding the bar never moves the
    /// page. While the keyboard is up the bar is behind it and nothing is kept.
    func reserveBottom(_ height: CGFloat) {
        bottomReserve = height
        applyObscuredInsets()
    }

    private func applyObscuredInsets() {
        let bottom = keyboardShown ? 0 : bottomReserve
        if webView.obscuredContentInsets.bottom != bottom {
            webView.obscuredContentInsets = UIEdgeInsets(top: 0, left: 0, bottom: bottom, right: 0)
        }
    }

    private func observeKeyboard() {
        let center = NotificationCenter.default
        center.addObserver(self, selector: #selector(keyboardWillShow), name: UIResponder.keyboardWillShowNotification, object: nil)
        center.addObserver(self, selector: #selector(keyboardWillHide), name: UIResponder.keyboardWillHideNotification, object: nil)
    }

    @objc private func keyboardWillShow() {
        keyboardShown = true
        applyObscuredInsets()
    }

    @objc private func keyboardWillHide() {
        keyboardShown = false
        applyObscuredInsets()
    }

    /// The page reported how far it is scrolled.
    fileprivate func pageScrolled(_ body: Any) {
        guard let fields = body as? [String: Any], let href = fields["href"] as? String, let url = URL(string: href),
              let target = AppURL.target(of: url), let scrollY = (fields["y"] as? NSNumber)?.doubleValue else {
            assertionFailure("the page's scroll report is not {href, y}: \(body)")
            return
        }
        rememberScroll(target, scrollY)
    }

    // MARK: Text size

    /// Show pages' text at `percent`: the page on screen at once, every later page as it loads.
    func setTextSize(_ percent: Int) {
        guard percent != textSizePercent else { return }
        textSizePercent = percent
        PageShims.install(in: webView.configuration.userContentController, textSizePercent: percent)
        webView.evaluateJavaScript(PageShims.styleScript(PageShims.appStyle(textSizePercent: percent))) { [weak self] _, error in
            // A page that is not the app's (none loaded yet) has nothing to restyle.
            guard let error, self?.webView.url != nil else { return }
            MainActor.assumeIsolated {
                self?.problem = Problem(title: "The text size could not be changed on this page",
                                        message: "\(error.localizedDescription) Reload the page to see it at the new size.")
            }
        }
    }

    /// "Library - Teacher" -> "Library".
    static func shortTitle(_ title: String?) -> String {
        guard let title else { return "" }
        return title.hasSuffix(" - Teacher") ? String(title.dropLast(" - Teacher".count)) : title
    }

    // MARK: Actions

    func load(target: String) {
        guard let url = AppURL.url(forTarget: target) else {
            problem = Problem(title: "This page could not be opened", message: "\(target) is not an address in Teacher.")
            return
        }
        webView.load(URLRequest(url: url))
    }

    /// Load `target` and, once it has finished loading, scroll it to `scrollY` (CSS pixels).
    func load(target: String, scrollY: Double?) {
        if let scrollY, scrollY > 0 { pendingScroll = (target, scrollY) }
        load(target: target)
    }

    /// The system's find bar for the page.
    func findOnPage() {
        webView.findInteraction?.presentFindNavigator(showingReplace: false)
    }

    func goBack() { webView.goBack() }
    func goForward() { webView.goForward() }

    func reload() {
        if webView.url == nil {
            load(target: target ?? "/")
        } else {
            webView.reload()
        }
    }

    @objc private func pulledToRefresh() {
        reload()
    }

    private func endRefreshing() {
        if refreshControl.isRefreshing { refreshControl.endRefreshing() }
    }
}

// MARK: - Navigation

extension WebViewHost: WKNavigationDelegate {
    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void
    ) {
        if navigationAction.shouldPerformDownload {
            // WebKit's downloads bypass the URL scheme handler ("unsupported URL"), so a
            // download of the app's own pages is fetched through the gateway instead.
            if let url = navigationAction.request.url, AppURL.target(of: url) != nil {
                decisionHandler(.cancel)
                download(url)
            } else {
                decisionHandler(.download)
            }
            return
        }
        let isMainFrame = navigationAction.targetFrame?.isMainFrame ?? true
        switch LinkPolicy.decide(navigationAction.request.url, isMainFrame: isMainFrame, server: server) {
        case .allow:
            decisionHandler(.allow)
        case .openInApp(let url):
            decisionHandler(.cancel)
            webView.load(URLRequest(url: url))
        case .openOutside(let url):
            decisionHandler(.cancel)
            openExternally(url)
        case .cancel:
            decisionHandler(.cancel)
        }
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationResponse: WKNavigationResponse,
        decisionHandler: @escaping @MainActor (WKNavigationResponsePolicy) -> Void
    ) {
        // A file the web view cannot show (a zip export) is downloaded and offered to share.
        if navigationResponse.canShowMIMEType {
            decisionHandler(.allow)
        } else if let url = navigationResponse.response.url, AppURL.target(of: url) != nil {
            decisionHandler(.cancel)
            download(url)
        } else {
            decisionHandler(.download)
        }
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
        // A new page (or one gone back to) starts with the bar showing, as in Safari.
        bar.reset()
        barVisible = true
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        endRefreshing()
        if let pending = pendingScroll {
            pendingScroll = nil
            // Only the page it was remembered for; a page that redirected elsewhere starts at the top.
            if target == pending.target {
                webView.evaluateJavaScript("window.airistotleRestoreScroll(\(pending.scrollY))") { [weak self] _, error in
                    guard let error else { return }
                    MainActor.assumeIsolated {
                        self?.problem = Problem(title: "The page could not scroll back to where you left it",
                                                message: error.localizedDescription)
                    }
                }
            }
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: any Error) {
        endRefreshing()
        pendingScroll = nil
        report(error, title: "This page did not finish loading", offersReload: true)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: any Error) {
        endRefreshing()
        pendingScroll = nil
        report(error, title: "This page could not be opened", offersReload: true)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        // The page's process was ended (memory pressure); load it again rather than leave a
        // blank screen.
        reload()
    }

    private func report(_ error: any Error, title: String, offersReload: Bool = false) {
        let nsError = error as NSError
        // A load that was replaced by another one (a link tapped while loading) is not a failure.
        if nsError.domain == NSURLErrorDomain, nsError.code == NSURLErrorCancelled { return }
        if nsError.domain == "WebKitErrorDomain", nsError.code == 102 { return } // frame load interrupted (a download)
        problem = Problem(title: title, message: error.localizedDescription, offersReload: offersReload)
    }
}

// MARK: - New windows and dialogs

extension WebViewHost: WKUIDelegate {
    /// `target="_blank"` links and `window.open`: Teacher's own pages open in this web view,
    /// anything else in Safari. No second web view is ever made.
    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        switch LinkPolicy.decide(navigationAction.request.url, isMainFrame: true, server: server) {
        case .allow:
            if let url = navigationAction.request.url, AppURL.target(of: url) != nil {
                webView.load(URLRequest(url: url))
            }
        case .openInApp(let url):
            webView.load(URLRequest(url: url))
        case .openOutside(let url):
            openExternally(url)
        case .cancel:
            break
        }
        return nil
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptAlertPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping @MainActor () -> Void
    ) {
        present(message, buttons: [("OK", .default, { completionHandler() })])
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptConfirmPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping @MainActor (Bool) -> Void
    ) {
        present(message, buttons: [
            ("Cancel", .cancel, { completionHandler(false) }),
            ("OK", .default, { completionHandler(true) })
        ])
    }

    private func present(_ message: String, buttons: [(String, UIAlertAction.Style, @MainActor () -> Void)]) {
        let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        for (title, style, action) in buttons {
            alert.addAction(UIAlertAction(title: title, style: style) { _ in MainActor.assumeIsolated { action() } })
        }
        guard let presenter = webView.window?.rootViewController?.topmostPresented else {
            buttons.first { $0.1 == .cancel }?.2() ?? buttons.first?.2()
            return
        }
        presenter.present(alert, animated: true)
    }
}

// MARK: - Downloads

extension WebViewHost {
    /// Fetch an app URL through the gateway, save it in a temporary folder and offer it to
    /// the share sheet. A failure (offline and not saved, a server error) is shown, with the
    /// server's own message when it gave one.
    func download(_ url: URL) {
        guard let target = AppURL.target(of: url) else { return }
        let gateway = gateway
        Task {
            let reply = await gateway.reply(to: GatewayRequest(method: "GET", target: target, headers: ["Accept": "*/*"]))
            do {
                let data = try await reply.collectBody()
                guard (200..<300).contains(reply.status) else {
                    let detail = AuthClient.detail(of: data) ?? "HTTP \(reply.status)"
                    problem = Problem(title: "The download failed", message: detail)
                    return
                }
                let name = Self.fileName(contentDisposition: reply.header("Content-Disposition"), url: url)
                let folder = FileManager.default.temporaryDirectory
                    .appending(path: "Downloads/\(UUID().uuidString)", directoryHint: .isDirectory)
                try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
                let file = folder.appending(path: name)
                try data.write(to: file)
                sharedFile = file
            } catch {
                problem = Problem(title: "The download failed", message: error.localizedDescription)
            }
        }
    }

    /// The file name a `Content-Disposition` header gives (`filename*=UTF-8''...` first), else
    /// the URL's last path component; never a path.
    nonisolated static func fileName(contentDisposition: String?, url: URL) -> String {
        var name: String?
        if let header = contentDisposition {
            for part in header.split(separator: ";").map({ $0.trimmingCharacters(in: .whitespaces) }) {
                let lowered = part.lowercased()
                if lowered.hasPrefix("filename*=") {
                    let value = part.dropFirst("filename*=".count)
                    let encoded = value.split(separator: "'", maxSplits: 2, omittingEmptySubsequences: false).last.map(String.init)
                    name = encoded?.removingPercentEncoding ?? name
                    break
                }
                if lowered.hasPrefix("filename=") {
                    name = String(part.dropFirst("filename=".count)).trimmingCharacters(in: CharacterSet(charactersIn: "\""))
                }
            }
        }
        let fallback = url.lastPathComponent.isEmpty || url.lastPathComponent == "/" ? "download" : url.lastPathComponent
        let chosen = (name?.isEmpty == false ? name : nil) ?? fallback
        let safe = chosen.replacingOccurrences(of: "/", with: "-").replacingOccurrences(of: "\\", with: "-")
        return safe.hasPrefix(".") ? "download\(safe)" : safe
    }
}

extension WebViewHost: WKDownloadDelegate {
    func download(
        _ download: WKDownload,
        decideDestinationUsing response: URLResponse,
        suggestedFilename: String,
        completionHandler: @escaping @MainActor (URL?) -> Void
    ) {
        let folder = FileManager.default.temporaryDirectory
            .appending(path: "Downloads/\(UUID().uuidString)", directoryHint: .isDirectory)
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        } catch {
            problem = Problem(title: "The download could not be saved", message: error.localizedDescription)
            completionHandler(nil)
            return
        }
        let name = suggestedFilename.isEmpty ? "download" : suggestedFilename
        let destination = folder.appending(path: name)
        downloads[ObjectIdentifier(download)] = destination
        completionHandler(destination)
    }

    func downloadDidFinish(_ download: WKDownload) {
        sharedFile = downloads.removeValue(forKey: ObjectIdentifier(download))
    }

    func download(_ download: WKDownload, didFailWithError error: any Error, resumeData: Data?) {
        downloads.removeValue(forKey: ObjectIdentifier(download))
        problem = Problem(title: "The download failed", message: error.localizedDescription)
    }
}

/// The script message handler, holding the host weakly (the web view's configuration keeps
/// its handlers for as long as the web view lives).
private final class ScriptMessageProxy: NSObject, WKScriptMessageHandler {
    weak var host: WebViewHost?

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame else { return }
        host?.pageScrolled(message.body)
    }
}

extension UIViewController {
    var topmostPresented: UIViewController {
        presentedViewController?.topmostPresented ?? self
    }
}

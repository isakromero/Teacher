import WebKit
import XCTest
@testable import Airistotle

/// The gateway behind a real WKWebView: what WebKit actually hands the scheme handler and what
/// the page gets back (MIME types for modules, request bodies, streaming, storage).
@MainActor
final class SchemeHandlerTests: XCTestCase {
    private var transport: FakeTransport!
    private var webView: WKWebView!
    private var handler: SchemeHandler!
    private var loads: NavigationWaiter!
    private var events: EventRecorder!
    private var scrollReports: ScrollReports!

    nonisolated private static let page = """
    <!doctype html><html><head><meta charset="utf-8"><title>Test - Teacher</title>
    <script type="module" src="/static/js/module.js"></script></head><body>
    <header class="site-header"><nav class="site-nav"><a class="brand" href="/">Teacher</a>
    <form class="nav-signout" method="post" action="/logout"><button type="submit">Sign out</button></form></nav></header>
    <p id="p">page</p><a id="link" href="/library">Library</a><div style="height: 3000px"></div></body></html>
    """

    override func setUp() async throws {
        transport = FakeTransport { request, _ in
            switch request.url?.path() {
            case "/": FakeTransport.answer(200, ["Content-Type": "text/html; charset=utf-8"], body: Self.page)
            case "/static/js/module.js":
                FakeTransport.answer(200, ["Content-Type": "text/javascript; charset=utf-8"], body: "window.moduleRan = 'yes';")
            default: FakeTransport.answer(404, ["Content-Type": "application/json"], body: "{\"detail\":\"Not Found\"}")
            }
        }
        events = EventRecorder()
        let gateway = Gateway(transport: transport, cache: ResponseCache(root: TestFiles.folder()), events: events.sink)
        gateway.connection = .test
        handler = SchemeHandler(gateway: gateway)
        let configuration = WKWebViewConfiguration()
        configuration.setURLSchemeHandler(handler, forURLScheme: AppURL.scheme)
        configuration.websiteDataStore = .nonPersistent()
        PageShims.install(in: configuration.userContentController, textSizePercent: 120)
        scrollReports = ScrollReports()
        configuration.userContentController.add(scrollReports, name: PageShims.scrollHandler)
        webView = WKWebView(frame: CGRect(x: 0, y: 0, width: 320, height: 480), configuration: configuration)
        loads = NavigationWaiter()
        webView.navigationDelegate = loads
        let window = UIApplication.shared.connectedScenes.compactMap { ($0 as? UIWindowScene)?.keyWindow }.first
        window?.addSubview(webView)
        webView.load(URLRequest(url: AppURL.root))
        try await loads.wait()
    }

    override func tearDown() async throws {
        webView.removeFromSuperview()
        webView = nil
    }

    private func run(_ body: String) async throws -> Any? {
        try await webView.callAsyncJavaScript(body, contentWorld: .page)
    }

    func testThePageAndItsModuleLoad() async throws {
        let title = try await run("return document.title")
        XCTAssertEqual(title as? String, "Test - Teacher")
        // A module runs only when served with a JavaScript MIME type.
        var ran: String?
        for _ in 0..<50 where ran == nil {
            ran = try await run("return window.moduleRan ?? null") as? String
            if ran == nil { try await Task.sleep(for: .milliseconds(50)) }
        }
        XCTAssertEqual(ran, "yes")
        let origin = try await run("return location.origin")
        XCTAssertEqual(origin as? String, "teacher://app")
        let navigation = try XCTUnwrap(transport.requests.first)
        XCTAssertEqual(navigation.value(forHTTPHeaderField: "Authorization"), "Bearer test-token")
    }

    func testJSONBodiesReachTheServer() async throws {
        transport.setHandler { request, _ in
            FakeTransport.answer(201, ["Content-Type": "application/json"], data: request.httpBody ?? Data("none".utf8))
        }
        let echoed = try await run("""
            const r = await fetch('/api/artifacts', {method: 'POST', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({sources: ['a']})});
            return r.status + ' ' + await r.text();
            """)
        XCTAssertEqual(echoed as? String, "201 {\"sources\":[\"a\"]}")
        let sent = try XCTUnwrap(transport.requests.last)
        XCTAssertEqual(sent.httpMethod, "POST")
        XCTAssertNil(sent.value(forHTTPHeaderField: "Origin"), "the app's origin would fail the server's cross-site check")
    }

    func testAFileUploadReachesTheServer() async throws {
        transport.setHandler { request, _ in
            let body = request.httpBody ?? Data()
            let type = request.value(forHTTPHeaderField: "Content-Type") ?? ""
            return FakeTransport.answer(200, ["Content-Type": "text/plain"], body: "\(body.count) \(type)")
        }
        let uploaded = try await run("""
            const form = new FormData();
            form.append('title', 'Notes');
            form.append('file', new File(['%PDF-1.4 hello'], 'paper.pdf', {type: 'application/pdf'}));
            const r = await fetch('/api/sources', {method: 'POST', body: form});
            return await r.text();
            """)
        let answer = try XCTUnwrap(uploaded as? String)
        XCTAssertTrue(answer.contains("multipart/form-data; boundary="), answer)
        let sent = try XCTUnwrap(transport.requests.last?.httpBody).text
        XCTAssertTrue(sent.contains("filename=\"paper.pdf\""), sent)
        XCTAssertTrue(sent.contains("%PDF-1.4 hello"), sent)
        XCTAssertTrue(sent.contains("Notes"), sent)
    }

    func testAnUploadWhoseBodyWebKitDroppedIsRefusedNotSentEmpty() async throws {
        let before = transport.requests.count
        // XMLHttpRequest is not covered by the fetch shim: WebKit drops the File body.
        let refused = try await run("""
            const form = new FormData();
            form.append('file', new File(['%PDF-1.4 hello'], 'paper.pdf', {type: 'application/pdf'}));
            const x = new XMLHttpRequest(); x.open('POST', '/api/sources', false); x.send(form);
            return x.status + ' ' + JSON.parse(x.responseText).detail;
            """)
        let answer = try XCTUnwrap(refused as? String)
        XCTAssertTrue(answer.hasPrefix("400 The app did not receive this request's content"), answer)
        XCTAssertEqual(transport.requests.count, before, "nothing reached the server")
    }

    func testAStreamedReplyReachesThePageBeforeItEnds() async throws {
        let (body, feed) = AsyncThrowingStream<Data, any Error>.makeStream()
        transport.setHandler { _, _ in
            let head = HTTPURLResponse(url: URL(string: "https://example.test/")!, statusCode: 200, httpVersion: nil,
                                       headerFields: ["Content-Type": "text/event-stream"])!
            return TransportResponse(response: head, body: body)
        }
        feed.yield(Data("event: delta\ndata: {\"text\":\"first\"}\n\n".utf8))
        // The script returns after the first piece; the stream is still open.
        let first = try await run("""
            const r = await fetch('/api/stream', {method: 'POST', headers: {Accept: 'text/event-stream'}, body: '{}'});
            window.streamReader = r.body.getReader();
            const {value} = await window.streamReader.read();
            return r.headers.get('content-type') + '|' + new TextDecoder().decode(value);
            """)
        XCTAssertEqual(first as? String, "text/event-stream|event: delta\ndata: {\"text\":\"first\"}\n\n")
        feed.yield(Data("event: done\ndata: {}\n\n".utf8))
        feed.finish()
        let rest = try await run("""
            let text = '';
            for (;;) { const {value, done} = await window.streamReader.read(); if (done) break; text += new TextDecoder().decode(value); }
            return text;
            """)
        XCTAssertEqual(rest as? String, "event: done\ndata: {}\n\n")
    }

    func testLocalStorageWorksOnTheAppOrigin() async throws {
        let value = try await run("localStorage.setItem('k', 'v'); return localStorage.getItem('k')")
        XCTAssertEqual(value as? String, "v")
    }

    func testOfflineErrorsReachThePageInTheAPIShape() async throws {
        transport.setHandler { _, _ in throw URLError(.notConnectedToInternet) }
        let refused = try await run("""
            const r = await fetch('/api/artifacts/x/responses', {method: 'POST', body: '{}'});
            return r.status + ' ' + (await r.json()).detail;
            """)
        let answer = try XCTUnwrap(refused as? String)
        XCTAssertTrue(answer.hasPrefix("503 The phone is offline"), answer)
        XCTAssertTrue(answer.hasSuffix("This was not sent to the server and nothing was saved."), answer)
    }
}

extension SchemeHandlerTests {
    func testTheAppStyleHidesSignOutAndScalesTheText() async throws {
        let display = try await run("return getComputedStyle(document.querySelector('.nav-signout')).display")
        XCTAssertEqual(display as? String, "none")
        let size = try await run("return parseFloat(getComputedStyle(document.documentElement).fontSize)")
        XCTAssertEqual(try XCTUnwrap((size as? NSNumber)?.doubleValue), 19.2, accuracy: 0.01, "120% of 16px")
        let callout = try await run("return getComputedStyle(document.getElementById('link')).webkitTouchCallout")
        XCTAssertEqual(callout as? String, "none")

        // A new size reaches the page on screen at once.
        try await webView.evaluateJavaScript(PageShims.styleScript(PageShims.appStyle(textSizePercent: 150)))
        let larger = try await run("return parseFloat(getComputedStyle(document.documentElement).fontSize)")
        XCTAssertEqual(try XCTUnwrap((larger as? NSNumber)?.doubleValue), 24, accuracy: 0.01)
        let styles = try await run("return document.querySelectorAll('#\(PageShims.styleID)').length")
        XCTAssertEqual(styles as? Int, 1, "the style is replaced, not added again")
    }

    func testTheScrollPositionIsReportedAndRestored() async throws {
        _ = try await run("window.scrollTo(0, 420); return null")
        for _ in 0..<40 where scrollReports.received.isEmpty { try await Task.sleep(for: .milliseconds(50)) }
        let report = try XCTUnwrap(scrollReports.received.last)
        XCTAssertEqual(report["href"] as? String, "teacher://app/")
        XCTAssertEqual((report["y"] as? NSNumber)?.intValue, 420)

        _ = try await run("window.scrollTo(0, 0); window.airistotleRestoreScroll(700); return null")
        var position = 0.0
        for _ in 0..<40 where position != 700 {
            position = (try await run("return window.scrollY") as? NSNumber)?.doubleValue ?? 0
            if position != 700 { try await Task.sleep(for: .milliseconds(50)) }
        }
        XCTAssertEqual(position, 700)
    }

    func testAGradedAnswerIsReadFromWhatThePageReceives() async throws {
        transport.setHandler { _, _ in
            FakeTransport.answer(201, ["Content-Type": "application/json"],
                                 body: "{\"attempt\":1,\"passed\":false,\"verdict\":\"failed\",\"feedback\":\"No.\"}")
        }
        let answered = try await run("""
            const r = await fetch('/api/artifacts/s/blocks/g1/responses', {method: 'POST',
              headers: {'Content-Type': 'application/json'}, body: JSON.stringify({response: {choice: 0}})});
            return r.status + ' ' + (await r.json()).verdict;
            """)
        XCTAssertEqual(answered as? String, "201 failed", "the page gets the reply unchanged")
        for _ in 0..<20 where !events.events.contains(.answerGraded(.failed)) { try await Task.sleep(for: .milliseconds(25)) }
        XCTAssertTrue(events.events.contains(.answerGraded(.failed)), "\(events.events)")
    }
}

/// Records the page's scroll reports.
@MainActor
final class ScrollReports: NSObject, WKScriptMessageHandler {
    private(set) var received: [[String: Any]] = []

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        if let body = message.body as? [String: Any] { received.append(body) }
    }
}

/// Resumes when the web view finishes (or fails) a load.
@MainActor
final class NavigationWaiter: NSObject, WKNavigationDelegate {
    private var continuation: CheckedContinuation<Void, any Error>?
    private var finished: Result<Void, any Error>?

    func wait() async throws {
        if let finished { return try finished.get() }
        try await withCheckedThrowingContinuation { continuation = $0 }
    }

    private func complete(_ result: Result<Void, any Error>) {
        if let continuation {
            self.continuation = nil
            continuation.resume(with: result)
        } else {
            finished = result
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { complete(.success(())) }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: any Error) { complete(.failure(error)) }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: any Error) {
        complete(.failure(error))
    }
}

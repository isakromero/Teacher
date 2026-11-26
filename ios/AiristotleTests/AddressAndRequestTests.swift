import XCTest
@testable import Airistotle

final class ServerAddressTests: XCTestCase {
    func testHTTPSAddressesAreNormalised() throws {
        XCTAssertEqual(try ServerAddress(parsing: "https://airistotle.fly.dev").description, "https://airistotle.fly.dev")
        XCTAssertEqual(try ServerAddress(parsing: "  Airistotle.FLY.dev/ ").description, "https://airistotle.fly.dev")
        XCTAssertEqual(try ServerAddress(parsing: "https://example.test:443").description, "https://example.test")
        XCTAssertEqual(try ServerAddress(parsing: "https://example.test:8443").description, "https://example.test:8443")
        XCTAssertEqual(ServerAddress.defaultAddress.description, "https://airistotle.fly.dev")
    }

    func testPlainHTTPOnlyForLocalAddresses() throws {
        XCTAssertEqual(try ServerAddress(parsing: "http://127.0.0.1:8031").description, "http://127.0.0.1:8031")
        XCTAssertNoThrow(try ServerAddress(parsing: "http://localhost:8000"))
        XCTAssertNoThrow(try ServerAddress(parsing: "http://192.168.1.20:8000"))
        XCTAssertNoThrow(try ServerAddress(parsing: "http://10.0.0.5"))
        XCTAssertNoThrow(try ServerAddress(parsing: "http://172.20.1.1"))
        XCTAssertNoThrow(try ServerAddress(parsing: "http://macbook.local:8000"))
        XCTAssertNoThrow(try ServerAddress(parsing: "http://devbox:8000"))
        XCTAssertThrowsError(try ServerAddress(parsing: "http://airistotle.fly.dev")) { error in
            XCTAssertEqual(error as? ServerAddress.ParseError, .plainHTTPNotLocal("airistotle.fly.dev"))
        }
        XCTAssertThrowsError(try ServerAddress(parsing: "http://172.32.0.1"))
        XCTAssertThrowsError(try ServerAddress(parsing: "http://fd.example.com"))
    }

    func testRefusesWhatCannotWork() {
        XCTAssertThrowsError(try ServerAddress(parsing: "  ")) { XCTAssertEqual($0 as? ServerAddress.ParseError, .empty) }
        XCTAssertThrowsError(try ServerAddress(parsing: "ftp://example.test")) {
            XCTAssertEqual($0 as? ServerAddress.ParseError, .unsupportedScheme("ftp"))
        }
        XCTAssertThrowsError(try ServerAddress(parsing: "https://example.test/teacher")) {
            XCTAssertEqual($0 as? ServerAddress.ParseError, .hasPath("/teacher"))
        }
        XCTAssertThrowsError(try ServerAddress(parsing: "https://me:pw@example.test")) {
            XCTAssertEqual($0 as? ServerAddress.ParseError, .hasCredentials)
        }
        XCTAssertThrowsError(try ServerAddress(parsing: "https://example.test/?a=1")) {
            XCTAssertEqual($0 as? ServerAddress.ParseError, .hasQueryOrFragment)
        }
    }

    func testOwnsOnlyItsOwnOrigin() throws {
        let server = try ServerAddress(parsing: "http://127.0.0.1:8031")
        XCTAssertTrue(server.owns(URL(string: "http://127.0.0.1:8031/library?x=1")!))
        XCTAssertFalse(server.owns(URL(string: "http://127.0.0.1:8000/library")!))
        XCTAssertFalse(server.owns(URL(string: "https://127.0.0.1:8031/library")!))
        XCTAssertFalse(server.owns(URL(string: "https://arxiv.org/abs/1706.03762")!))
    }

    func testCodableValidatesAgain() throws {
        let data = try JSONEncoder().encode(ServerAddress.test)
        XCTAssertEqual(data.text, "\"https:\\/\\/example.test\"")
        XCTAssertEqual(try JSONDecoder().decode(ServerAddress.self, from: data), .test)
        XCTAssertThrowsError(try JSONDecoder().decode(ServerAddress.self, from: Data("\"http://example.com\"".utf8)))
    }
}

final class AppURLTests: XCTestCase {
    func testTargetsRoundTrip() {
        XCTAssertEqual(AppURL.url(forTarget: "/library")?.absoluteString, "teacher://app/library")
        XCTAssertEqual(AppURL.target(of: URL(string: "teacher://app/")!), "/")
        XCTAssertEqual(AppURL.target(of: URL(string: "teacher://app")!), "/")
        XCTAssertEqual(AppURL.target(of: URL(string: "teacher://app/api/x%20y?q=a%26b&n=1")!), "/api/x%20y?q=a%26b&n=1")
        XCTAssertEqual(AppURL.target(of: URL(string: "teacher://app/artifacts/s#block-3")!), "/artifacts/s")
        XCTAssertNil(AppURL.target(of: URL(string: "teacher://other/x")!))
        XCTAssertNil(AppURL.target(of: URL(string: "https://app/x")!))
        XCTAssertNil(AppURL.url(forTarget: "library"))
    }

    func testServerLinksMapIntoTheApp() {
        let server = ServerAddress.test
        XCTAssertEqual(
            AppURL.appURL(forServerURL: URL(string: "https://example.test/artifacts/s?x=1#b")!, server: server)?.absoluteString,
            "teacher://app/artifacts/s?x=1#b"
        )
        XCTAssertNil(AppURL.appURL(forServerURL: URL(string: "https://elsewhere.test/a")!, server: server))
    }
}

final class GatewayRequestTests: XCTestCase {
    func testDocumentLoadsAreNavigations() {
        var urlRequest = URLRequest(url: URL(string: "teacher://app/library?sort=new")!)
        urlRequest.setValue("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", forHTTPHeaderField: "Accept")
        urlRequest.mainDocumentURL = urlRequest.url
        let request = GatewayRequest(urlRequest, body: nil)
        XCTAssertEqual(request?.method, "GET")
        XCTAssertEqual(request?.target, "/library?sort=new")
        XCTAssertEqual(request?.path, "/library")
        XCTAssertEqual(request?.queryItems, [URLQueryItem(name: "sort", value: "new")])
        XCTAssertEqual(request?.isNavigation, true)
        XCTAssertEqual(request?.isMainFrame, true)
    }

    func testScriptFetchesAreNotNavigations() {
        var urlRequest = URLRequest(url: URL(string: "teacher://app/api/artifacts")!)
        urlRequest.httpMethod = "post"
        urlRequest.setValue("application/json", forHTTPHeaderField: "Accept")
        urlRequest.mainDocumentURL = URL(string: "teacher://app/library")
        let request = GatewayRequest(urlRequest, body: Data("{}".utf8))
        XCTAssertEqual(request?.method, "POST")
        XCTAssertEqual(request?.isNavigation, false)
        XCTAssertEqual(request?.isMainFrame, false)
        XCTAssertEqual(request?.body, Data("{}".utf8))
        XCTAssertNil(GatewayRequest(URLRequest(url: URL(string: "https://example.test/")!), body: nil))
    }

    func testFrameDocumentIsNotTheMainFrame() {
        var urlRequest = URLRequest(url: URL(string: "teacher://app/static/frame.html")!)
        urlRequest.setValue("text/html", forHTTPHeaderField: "Accept")
        urlRequest.mainDocumentURL = URL(string: "teacher://app/labs/x")
        let request = GatewayRequest(urlRequest, body: nil)
        XCTAssertEqual(request?.isNavigation, true)
        XCTAssertEqual(request?.isMainFrame, false)
    }

    func testServerRequestCarriesTheTokenAndDropsBrowserHeaders() {
        let gateway = Gateway(transport: FakeTransport(), cache: ResponseCache(root: TestFiles.folder()), events: { _ in })
        let request = GatewayRequest(
            method: "POST",
            target: "/api/history/heartbeat?x=1",
            headers: [
                "Origin": "teacher://app", "Referer": "teacher://app/library", "Cookie": "a=b",
                "Authorization": "Bearer page-supplied", "If-None-Match": "\"etag\"",
                "Content-Type": "application/json", "Accept": "application/json", "X-Custom": "kept"
            ],
            body: Data("{\"a\":1}".utf8)
        )
        let urlRequest = gateway.serverRequest(for: request, connection: .test)
        XCTAssertEqual(urlRequest?.url?.absoluteString, "https://example.test/api/history/heartbeat?x=1")
        XCTAssertEqual(urlRequest?.httpMethod, "POST")
        XCTAssertEqual(urlRequest?.value(forHTTPHeaderField: "Authorization"), "Bearer test-token")
        XCTAssertNil(urlRequest?.value(forHTTPHeaderField: "Origin"))
        XCTAssertNil(urlRequest?.value(forHTTPHeaderField: "Referer"))
        XCTAssertNil(urlRequest?.value(forHTTPHeaderField: "Cookie"))
        XCTAssertNil(urlRequest?.value(forHTTPHeaderField: "If-None-Match"))
        XCTAssertEqual(urlRequest?.value(forHTTPHeaderField: "Content-Type"), "application/json")
        XCTAssertEqual(urlRequest?.value(forHTTPHeaderField: "X-Custom"), "kept")
        XCTAssertEqual(urlRequest?.httpBody, Data("{\"a\":1}".utf8))
    }

    func testOpenServerGetsNoAuthorization() throws {
        let gateway = Gateway(transport: FakeTransport(), cache: ResponseCache(root: TestFiles.folder()), events: { _ in })
        let open = GatewayConnection(server: .test, token: nil, account: nil)
        let urlRequest = gateway.serverRequest(for: GatewayRequest(method: "GET", target: "/"), connection: open)
        XCTAssertNil(urlRequest?.value(forHTTPHeaderField: "Authorization"))
        XCTAssertNil(urlRequest?.httpBody)
    }
}

final class LinkPolicyTests: XCTestCase {
    func testWhereLinksGo() {
        let server = ServerAddress.test
        let decide = { (url: String, main: Bool) in LinkPolicy.decide(URL(string: url), isMainFrame: main, server: server) }
        XCTAssertEqual(decide("teacher://app/library", true), .allow)
        XCTAssertEqual(decide("teacher://evil/x", true), .cancel)
        XCTAssertEqual(decide("about:srcdoc", false), .allow)
        XCTAssertEqual(decide("about:blank", true), .allow)
        XCTAssertEqual(decide("https://example.test/library", true), .openInApp(URL(string: "teacher://app/library")!))
        XCTAssertEqual(decide("https://arxiv.org/abs/1", true), .openOutside(URL(string: "https://arxiv.org/abs/1")!))
        XCTAssertEqual(decide("https://www.youtube.com/embed/x", false), .allow)
        XCTAssertEqual(decide("mailto:a@b.test", true), .openOutside(URL(string: "mailto:a@b.test")!))
        XCTAssertEqual(decide("tel:123", false), .cancel)
        XCTAssertEqual(LinkPolicy.decide(nil, isMainFrame: true, server: server), .cancel)
    }
}

final class DownloadNameTests: XCTestCase {
    func testFileNamesComeFromTheHeaderAndNeverFormAPath() {
        let url = URL(string: "teacher://app/api/history/export?format=json")!
        let plain = "attachment; filename=\"teacher-history.json\""
        XCTAssertEqual(WebViewHost.fileName(contentDisposition: plain, url: url), "teacher-history.json")
        XCTAssertEqual(WebViewHost.fileName(contentDisposition: "attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.md", url: url), "résumé.md")
        let traversal = "attachment; filename=\"../../etc/passwd\""
        XCTAssertEqual(WebViewHost.fileName(contentDisposition: traversal, url: url), "download..-..-etc-passwd")
        XCTAssertEqual(WebViewHost.fileName(contentDisposition: nil, url: url), "export")
        XCTAssertEqual(WebViewHost.fileName(contentDisposition: nil, url: URL(string: "teacher://app/")!), "download")
    }
}

import XCTest
@testable import Airistotle

final class ResponseCacheTests: XCTestCase {
    func testKeysAreStableAndDistinct() {
        XCTAssertEqual(ResponseCache.key(for: "/library"), ResponseCache.key(for: "/library"))
        XCTAssertNotEqual(ResponseCache.key(for: "/library"), ResponseCache.key(for: "/library?x=1"))
        XCTAssertEqual(ResponseCache.key(for: "/").count, 64)
        // sha256("/"), so the files stay readable across app versions.
        XCTAssertEqual(ResponseCache.key(for: "/"), "8a5edab282632443219e051e4ade2d1d5bbc671c781051bf1437897cbdfea0f1")
    }

    func testNamespacesSeparateServersAndAccounts() throws {
        let other = try ServerAddress(parsing: "https://other.test")
        let one = ResponseCache.namespace(server: .test, account: "user-1")
        XCTAssertEqual(one, ResponseCache.namespace(server: .test, account: "user-1"))
        XCTAssertNotEqual(one, ResponseCache.namespace(server: .test, account: "user-2"))
        XCTAssertNotEqual(one, ResponseCache.namespace(server: .test, account: nil))
        XCTAssertNotEqual(one, ResponseCache.namespace(server: other, account: "user-1"))
    }

    func testCommitMakesACopyVisible() async throws {
        let cache = ResponseCache(root: TestFiles.folder())
        let writer = try cache.writer(namespace: "ns", target: "/library", status: 200, headers: ["Content-Type": "text/html"])
        try writer.append(Data("<p>a".utf8))
        XCTAssertNil(cache.lookup(namespace: "ns", target: "/library"), "nothing is visible before commit")
        try writer.append(Data("b</p>".utf8))
        try writer.commit()

        let saved = try XCTUnwrap(cache.lookup(namespace: "ns", target: "/library"))
        XCTAssertEqual(saved.metadata.status, 200)
        XCTAssertEqual(saved.metadata.headers["Content-Type"], "text/html")
        XCTAssertEqual(saved.metadata.byteCount, 9)
        XCTAssertEqual(try Data(contentsOf: saved.bodyFile).text, "<p>ab</p>")
        var streamed = Data()
        for try await chunk in ResponseCache.bodyStream(of: saved.bodyFile, chunkSize: 4) { streamed.append(chunk) }
        XCTAssertEqual(streamed.text, "<p>ab</p>")

        XCTAssertNil(cache.lookup(namespace: "other", target: "/library"))
        XCTAssertNil(cache.lookup(namespace: "ns", target: "/library?x=1"))
        XCTAssertEqual(cache.entryCount(), 1)
        XCTAssertGreaterThan(cache.usage(), 0)
    }

    func testANewCopyReplacesTheOld() throws {
        let cache = ResponseCache(root: TestFiles.folder())
        for body in ["old", "new"] {
            let writer = try cache.writer(namespace: "ns", target: "/a", status: 200, headers: [:])
            try writer.append(Data(body.utf8))
            try writer.commit()
        }
        let saved = try XCTUnwrap(cache.lookup(namespace: "ns", target: "/a"))
        XCTAssertEqual(try Data(contentsOf: saved.bodyFile).text, "new")
        XCTAssertEqual(cache.entryCount(), 1)
    }

    func testDiscardLeavesNothing() throws {
        let cache = ResponseCache(root: TestFiles.folder())
        let writer = try cache.writer(namespace: "ns", target: "/a", status: 200, headers: [:])
        try writer.append(Data("partial".utf8))
        writer.discard()
        try writer.commit() // after discard: no effect
        XCTAssertNil(cache.lookup(namespace: "ns", target: "/a"))
        let partials = try FileManager.default.contentsOfDirectory(atPath: cache.root.appending(path: ".partial").path)
        XCTAssertEqual(partials, [])
    }

    func testClearRemovesEverythingAndTheRootIsNotBackedUp() throws {
        let cache = ResponseCache(root: TestFiles.folder().appending(path: "cache"))
        let writer = try cache.writer(namespace: "ns", target: "/a", status: 200, headers: [:])
        try writer.append(Data("x".utf8))
        try writer.commit()
        let values = try cache.root.resourceValues(forKeys: [.isExcludedFromBackupKey])
        XCTAssertEqual(values.isExcludedFromBackup, true)
        try cache.clear()
        XCTAssertNil(cache.lookup(namespace: "ns", target: "/a"))
        XCTAssertEqual(cache.usage(), 0)
        XCTAssertEqual(cache.entryCount(), 0)
        try cache.clear() // clearing an empty cache is fine
    }

    func testDefaultRootIsInApplicationSupport() throws {
        let root = try ResponseCache.defaultRoot()
        XCTAssertTrue(root.path.contains("/Library/Application Support/Airistotle/ResponseCache"), root.path)
        XCTAssertFalse(root.path.contains("/Caches/"))
    }
}

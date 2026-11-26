import Foundation
@testable import Airistotle

/// A transport whose answers the test scripts; it records every request it was given.
final class FakeTransport: HTTPTransport, @unchecked Sendable {
    typealias Handler = @Sendable (URLRequest, Bool) async throws -> TransportResponse

    private let lock = NSLock()
    private var recorded: [(request: URLRequest, followRedirects: Bool)] = []
    private var handler: Handler

    init(_ handler: @escaping Handler = { _, _ in throw URLError(.notConnectedToInternet) }) {
        self.handler = handler
    }

    var requests: [URLRequest] { lock.withLock { recorded.map(\.request) } }
    var followFlags: [Bool] { lock.withLock { recorded.map(\.followRedirects) } }

    func setHandler(_ handler: @escaping Handler) {
        lock.withLock { self.handler = handler }
    }

    func send(_ request: URLRequest, followRedirects: Bool) async throws -> TransportResponse {
        let handler = lock.withLock {
            recorded.append((request, followRedirects))
            return self.handler
        }
        return try await handler(request, followRedirects)
    }

    /// A complete answer.
    static func answer(_ status: Int, _ headers: [String: String] = [:], body: String = "", url: URL? = nil) -> TransportResponse {
        answer(status, headers, data: Data(body.utf8), url: url)
    }

    static func answer(_ status: Int, _ headers: [String: String] = [:], data: Data, url: URL? = nil) -> TransportResponse {
        let response = HTTPURLResponse(
            url: url ?? URL(string: "https://example.test/")!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: headers
        )!
        return TransportResponse(response: response, body: .single(data))
    }
}

/// Collects the gateway's events.
final class EventRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var recorded: [GatewayEvent] = []

    var events: [GatewayEvent] { lock.withLock { recorded } }

    var sink: @Sendable (GatewayEvent) -> Void {
        { [self] event in lock.withLock { recorded.append(event) } }
    }
}

enum TestFiles {
    /// A new, empty folder under the test run's temporary directory.
    static func folder(_ name: String = #function) -> URL {
        let url = FileManager.default.temporaryDirectory
            .appending(path: "AiristotleTests/\(name.filter { $0.isLetter || $0.isNumber })-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }
}

extension ServerAddress {
    static let test = try! ServerAddress(parsing: "https://example.test") // swiftlint:disable:this force_try
}

extension GatewayConnection {
    static let test = GatewayConnection(server: .test, token: "test-token", account: "user-1")
}

extension Data {
    var text: String { String(bytes: self, encoding: .utf8) ?? "<not UTF-8>" }
}

/// Decode `{"detail": "..."}`.
func detail(of data: Data) -> String? {
    (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
}

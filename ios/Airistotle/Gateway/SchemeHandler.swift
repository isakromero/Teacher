import Foundation
import WebKit

/// Hands every `teacher://app/...` load of the web view to the `Gateway` and feeds its reply
/// back to WebKit: the head first, then each body chunk as it arrives (so a streamed tutor
/// reply shows piece by piece), then the end. WebKit's callbacks and the task's methods are
/// all used on the main actor; once WebKit stops a task, nothing more is sent to it.
@MainActor
final class SchemeHandler: NSObject, WKURLSchemeHandler {
    private let gateway: Gateway
    private var running: [ObjectIdentifier: Task<Void, Never>] = [:]

    init(gateway: Gateway) {
        self.gateway = gateway
    }

    func webView(_ webView: WKWebView, start urlSchemeTask: any WKURLSchemeTask) {
        let id = ObjectIdentifier(urlSchemeTask)
        let urlRequest = urlSchemeTask.request
        let gateway = gateway
        running[id] = Task {
            let body: Data?
            do {
                body = try await Self.body(of: urlRequest)
            } catch {
                await deliver(id, urlSchemeTask, GatewayReply.jsonError(
                    400, "The app could not read this request's body (\(error.localizedDescription)); nothing was sent."
                ), url: urlRequest.url)
                return
            }
            guard let request = GatewayRequest(urlRequest, body: body) else {
                fail(id, urlSchemeTask, URLError(.unsupportedURL))
                return
            }
            if Self.bodyWentMissing(request) {
                await deliver(id, urlSchemeTask, GatewayReply.jsonError(
                    400, "The app did not receive this request's content from the page (WebKit passes no body "
                        + "for some uploads), so nothing was sent to the server."
                ), url: urlRequest.url)
                return
            }
            let reply = await gateway.reply(to: request)
            await deliver(id, urlSchemeTask, reply, url: urlRequest.url, headOnly: request.method == "HEAD")
        }
    }

    func webView(_ webView: WKWebView, stop urlSchemeTask: any WKURLSchemeTask) {
        running.removeValue(forKey: ObjectIdentifier(urlSchemeTask))?.cancel()
    }

    /// Send `reply` to WebKit: the head, each body chunk as it arrives, the end. Runs in the
    /// task `stop` cancels; nothing is sent once the task is no longer running.
    private func deliver(
        _ id: ObjectIdentifier,
        _ task: any WKURLSchemeTask,
        _ reply: GatewayReply,
        url: URL?,
        headOnly: Bool = false
    ) async {
        guard running[id] != nil, let url else { return }
        let response = HTTPURLResponse(url: url, statusCode: reply.status, httpVersion: "HTTP/1.1", headerFields: reply.headers)
        guard let response else {
            fail(id, task, URLError(.badServerResponse))
            return
        }
        task.didReceive(response)
        do {
            if !headOnly {
                for try await chunk in reply.body {
                    guard running[id] != nil else { return }
                    task.didReceive(chunk)
                }
            }
            guard running.removeValue(forKey: id) != nil else { return }
            task.didFinish()
        } catch {
            fail(id, task, error)
        }
    }

    private func fail(_ id: ObjectIdentifier, _ task: any WKURLSchemeTask, _ error: any Error) {
        guard running.removeValue(forKey: id) != nil else { return }
        task.didFailWithError(error)
    }

    /// A request that says it has content (a `Content-Type`) but reached the app without any:
    /// WebKit drops Blob and File bodies for custom schemes (`PageShims.fetchBodies` covers
    /// `fetch`). Sending it on empty would be a silent, wrong upload.
    nonisolated static func bodyWentMissing(_ request: GatewayRequest) -> Bool {
        !request.isReadOnly && request.body == nil && request.header("Content-Type") != nil
    }

    /// The request body: WebKit passes small bodies as `httpBody` and others (a file upload's
    /// form data) as a stream, which is read to the end off the main thread.
    nonisolated static func body(of request: URLRequest) async throws -> Data? {
        if let data = request.httpBody { return data }
        guard let stream = request.httpBodyStream else { return nil }
        let box = StreamBox(stream)
        return try await Task.detached { try box.readAll() }.value
    }
}

/// An input stream read once, on one thread.
private final class StreamBox: @unchecked Sendable {
    private let stream: InputStream

    init(_ stream: InputStream) {
        self.stream = stream
    }

    func readAll() throws -> Data {
        stream.open()
        defer { stream.close() }
        var data = Data()
        let size = 64 * 1024
        var buffer = [UInt8](repeating: 0, count: size)
        while true {
            let count = stream.read(&buffer, maxLength: size)
            if count < 0 { throw stream.streamError ?? URLError(.cannotDecodeContentData) }
            if count == 0 { break }
            data.append(buffer, count: count)
        }
        return data
    }
}

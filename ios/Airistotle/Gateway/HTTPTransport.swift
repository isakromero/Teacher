import Foundation
import Synchronization

/// The server's answer: its head, and the body as it arrives.
struct TransportResponse: Sendable {
    var response: HTTPURLResponse
    var body: AsyncThrowingStream<Data, any Error>
}

/// Sends one HTTP request. `send` returns once the response head has arrived and throws when
/// no response came (the server could not be reached, TLS failed, the request was cancelled).
protocol HTTPTransport: Sendable {
    /// `followRedirects` false hands a 3xx back as the response instead of following it.
    func send(_ request: URLRequest, followRedirects: Bool) async throws -> TransportResponse
}

extension HTTPTransport {
    /// The whole response (small JSON calls such as the sign-in routes).
    func data(for request: URLRequest) async throws -> (HTTPURLResponse, Data) {
        let reply = try await send(request, followRedirects: true)
        var data = Data()
        for try await chunk in reply.body { data.append(chunk) }
        return (reply.response, data)
    }
}

/// `HTTPTransport` over a `URLSession` whose delegate hands each body chunk on as it arrives,
/// so a streamed reply (the tutor's server-sent events) reaches the page piece by piece.
final class URLSessionTransport: HTTPTransport {
    private let delegate: SessionDelegate
    private let session: URLSession

    init(configuration: URLSessionConfiguration = URLSessionTransport.defaultConfiguration()) {
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        queue.name = "dev.airistotle.transport"
        delegate = SessionDelegate()
        session = URLSession(configuration: configuration, delegate: delegate, delegateQueue: queue)
    }

    deinit {
        session.invalidateAndCancel() // URLSession keeps its delegate until invalidated
    }

    static func defaultConfiguration() -> URLSessionConfiguration {
        let configuration = URLSessionConfiguration.ephemeral // the gateway keeps its own cache
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.urlCache = nil
        configuration.httpCookieStorage = nil // the server's session is the bearer token
        configuration.httpShouldSetCookies = false
        configuration.waitsForConnectivity = false // offline must be known now, not later
        configuration.timeoutIntervalForRequest = 60 // idle time between pieces of a response
        configuration.timeoutIntervalForResource = 60 * 60 // a long upload or a long reply
        return configuration
    }

    func send(_ request: URLRequest, followRedirects: Bool) async throws -> TransportResponse {
        let task = session.dataTask(with: request)
        let identifier = task.taskIdentifier
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (head: CheckedContinuation<TransportResponse, any Error>) in
                delegate.register(identifier, followRedirects: followRedirects, head: head)
                task.resume()
            }
        } onCancel: {
            task.cancel()
        }
    }
}

/// Routes the session's callbacks to the request each belongs to.
private final class SessionDelegate: NSObject, URLSessionDataDelegate, Sendable {
    private struct TaskState {
        var followRedirects: Bool
        var head: CheckedContinuation<TransportResponse, any Error>?
        var body: AsyncThrowingStream<Data, any Error>.Continuation?
    }

    private let states = Mutex<[Int: TaskState]>([:])

    func register(_ identifier: Int, followRedirects: Bool, head: CheckedContinuation<TransportResponse, any Error>) {
        states.withLock { $0[identifier] = TaskState(followRedirects: followRedirects, head: head, body: nil) }
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping @Sendable (URLRequest?) -> Void
    ) {
        let follow = states.withLock { $0[task.taskIdentifier]?.followRedirects ?? true }
        completionHandler(follow ? request : nil)
    }

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive response: URLResponse,
        completionHandler: @escaping @Sendable (URLSession.ResponseDisposition) -> Void
    ) {
        guard let http = response as? HTTPURLResponse else {
            fail(dataTask.taskIdentifier, URLError(.badServerResponse))
            completionHandler(.cancel)
            return
        }
        let (stream, continuation) = AsyncThrowingStream<Data, any Error>.makeStream()
        continuation.onTermination = { termination in
            if case .cancelled = termination { dataTask.cancel() }
        }
        let head = states.withLock { states -> CheckedContinuation<TransportResponse, any Error>? in
            let head = states[dataTask.taskIdentifier]?.head
            states[dataTask.taskIdentifier]?.head = nil
            states[dataTask.taskIdentifier]?.body = continuation
            return head
        }
        head?.resume(returning: TransportResponse(response: http, body: stream))
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        let body = states.withLock { $0[dataTask.taskIdentifier]?.body }
        body?.yield(data)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: (any Error)?) {
        if let error {
            fail(task.taskIdentifier, error)
            return
        }
        let state = states.withLock { $0.removeValue(forKey: task.taskIdentifier) }
        state?.body?.finish()
        state?.head?.resume(throwing: URLError(.badServerResponse))
    }

    private func fail(_ identifier: Int, _ error: any Error) {
        let state = states.withLock { $0.removeValue(forKey: identifier) }
        state?.head?.resume(throwing: error)
        state?.body?.finish(throwing: error)
    }
}

extension URLError {
    /// Errors that mean the server could not be reached at all (as opposed to a server that
    /// answered, or a request that was refused locally).
    var meansUnreachable: Bool {
        switch code {
        case .notConnectedToInternet, .networkConnectionLost, .cannotConnectToHost, .cannotFindHost,
             .dnsLookupFailed, .timedOut, .internationalRoamingOff, .dataNotAllowed,
             .callIsActive, .cannotLoadFromNetwork:
            true
        default:
            false
        }
    }
}

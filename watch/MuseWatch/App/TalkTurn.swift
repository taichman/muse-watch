import Foundation

/// One push-to-talk turn against the bridge's `POST /v1/talk`.
///
/// The request body is a live stream of microphone audio: `send` while the
/// button is held, `finishSending` on release. The response streams Muse's
/// reply back as it is spoken. Plain HTTP works everywhere on watchOS, unlike
/// WebSockets.
final class TalkTurn: NSObject, URLSessionDataDelegate {
    enum Outcome {
        case replied
        case noReply
        case failed(String)
    }

    private let onAudio: (Data) -> Void
    private let onFinish: (Outcome) -> Void
    private var session: URLSession!
    private var task: URLSessionUploadTask?
    private var bodyInput: InputStream?
    private var bodyOutput: OutputStream?

    // Upload buffer shared between the audio thread and the writer thread.
    private let condition = NSCondition()
    private var pending = Data()
    private var closed = false
    private let maxPending = 24_000 * 2 * 10  // drop audio beyond 10 s of backlog

    private var status = 0
    private var receivedAudio = false
    private var finished = false

    init(onAudio: @escaping (Data) -> Void, onFinish: @escaping (Outcome) -> Void) {
        self.onAudio = onAudio
        self.onFinish = onFinish
        super.init()
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 90  // Muse can think for a while before speaking
        config.timeoutIntervalForResource = 300
        session = URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }

    func start(settings: BridgeSettings) {
        guard let base = settings.baseURL else {
            finish(.failed("Set the bridge URL"))
            return
        }
        Stream.getBoundStreams(withBufferSize: 64 * 1024, inputStream: &bodyInput, outputStream: &bodyOutput)
        var request = URLRequest(url: base.appendingPathComponent("v1/talk"))
        request.httpMethod = "POST"
        request.setValue("Bearer \(settings.secret)", forHTTPHeaderField: "Authorization")
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        let task = session.uploadTask(withStreamedRequest: request)
        self.task = task
        Thread { [weak self] in self?.writeBody() }.start()
        task.resume()
    }

    /// Microphone audio; safe to call from the audio thread.
    func send(_ pcm: Data) {
        condition.lock()
        if !closed && pending.count < maxPending {
            pending.append(pcm)
            condition.signal()
        }
        condition.unlock()
    }

    /// The button was released: end the upload so Muse can answer.
    func finishSending() {
        condition.lock()
        closed = true
        condition.signal()
        condition.unlock()
    }

    func cancel() {
        finishSending()
        task?.cancel()
        session.invalidateAndCancel()
    }

    private func writeBody() {
        guard let output = bodyOutput else { return }
        output.open()
        defer { output.close() }
        while true {
            condition.lock()
            while pending.isEmpty && !closed { condition.wait() }
            if pending.isEmpty && closed {
                condition.unlock()
                return
            }
            let chunk = pending
            pending.removeAll(keepingCapacity: true)
            condition.unlock()

            var offset = 0
            while offset < chunk.count {
                let written = chunk.withUnsafeBytes { raw in
                    output.write(raw.baseAddress!.advanced(by: offset).assumingMemoryBound(to: UInt8.self),
                                 maxLength: chunk.count - offset)
                }
                if written < 0 { return }  // the request was cancelled or failed
                if written == 0 {
                    Thread.sleep(forTimeInterval: 0.005)
                } else {
                    offset += written
                }
            }
        }
    }

    private func finish(_ outcome: Outcome) {
        guard !finished else { return }
        finished = true
        DispatchQueue.main.async { self.onFinish(outcome) }
        session.finishTasksAndInvalidate()
    }

    // MARK: URLSessionDataDelegate

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    needNewBodyStream completionHandler: @escaping (InputStream?) -> Void) {
        completionHandler(bodyInput)
        bodyInput = nil  // a bound stream can be read once
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        status = (response as? HTTPURLResponse)?.statusCode ?? 0
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard status == 200 else { return }
        receivedAudio = true
        DispatchQueue.main.async { self.onAudio(data) }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        finishSending()
        if let error = error as? URLError, error.code != .cancelled {
            finish(.failed(Self.describe(error)))
            return
        }
        switch status {
        case 200: finish(receivedAudio ? .replied : .noReply)
        case 204: finish(.noReply)
        case 401: finish(.failed("Wrong bridge secret"))
        case 409: finish(.failed("Mac isn't paired with Muse"))
        case 502: finish(.failed("Muse unavailable"))
        case 0: finish(.failed("Cancelled"))
        default: finish(.failed("Bridge error \(status)"))
        }
    }

    private static func describe(_ error: URLError) -> String {
        switch error.code {
        case .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed: return "Can't reach the Mac"
        case .notConnectedToInternet, .networkConnectionLost: return "No connection"
        case .timedOut: return "Muse took too long"
        default: return error.localizedDescription
        }
    }
}

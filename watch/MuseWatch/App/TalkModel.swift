import Foundation
#if os(watchOS)
import WatchKit
#endif

/// Push-to-talk state machine.
///
/// - Hold the button to talk; release to send.
/// - A quick tap starts hands-free mode: talk, then tap again (or just stop
///   talking for a moment) to send. Siri and the Action
///   Button start in hands-free mode, since no finger is on the button.
/// - Pressing while Muse is speaking interrupts it and starts a new turn.
@MainActor
final class TalkModel: ObservableObject {
    static let shared = TalkModel()

    enum Phase: Equatable {
        case idle
        case listening
        case thinking
        case speaking
        case failed(String)
    }

    struct Turn: Identifiable {
        let id = UUID()
        let at: Date
        var spoke: TimeInterval
        var outcome: String
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var level: Float = 0
    @Published private(set) var turns: [Turn] = []
    @Published var settings = BridgeSettings.load()

    private let audio = AudioPipeline()
    private var turn: TalkTurn?
    private var handsFree = false
    private var listenStarted = Date()
    private var heardSpeech = false
    private var lastSpeech = Date()
    private var silenceWatch: Task<Void, Never>?

    private let tapThreshold: TimeInterval = 0.35
    private let speechRMS: Float = 0.02
    private let handsFreeSilence: TimeInterval = 1.6
    private let maxTurn: TimeInterval = 90

    // MARK: Gestures

    func pressBegan() {
        switch phase {
        case .listening where handsFree:
            stopListening()
        case .listening:
            break
        case .thinking, .speaking:
            interrupt()
            startListening(handsFree: false)
        case .idle, .failed:
            startListening(handsFree: false)
        }
    }

    func pressEnded(heldFor: TimeInterval) {
        guard phase == .listening, !handsFree else { return }
        if heldFor < tapThreshold {
            handsFree = true  // a tap: keep listening until a second tap or silence
        } else {
            stopListening()
        }
    }

    /// Start a hands-free turn (Siri, Action Button).
    func startHandsFree() {
        switch phase {
        case .listening: return
        case .thinking, .speaking: interrupt()
        case .idle, .failed: break
        }
        startListening(handsFree: true)
    }

    // MARK: Turn lifecycle

    private func startListening(handsFree: Bool) {
        guard settings.isComplete else {
            phase = .failed("Set up the bridge in Settings")
            return
        }
        self.handsFree = handsFree
        phase = .listening
        heardSpeech = false
        listenStarted = Date()
        lastSpeech = Date()
        haptic(.start)

        Task {
            guard await AudioPipeline.requestPermission() else {
                phase = .failed("Allow microphone access in Settings")
                return
            }
            guard phase == .listening else { return }
            do {
                try audio.start()
            } catch {
                phase = .failed("Audio unavailable")
                return
            }
            let turn = TalkTurn(
                onAudio: { [weak self] data in self?.received(data) },
                onFinish: { [weak self] outcome in self?.finished(outcome) }
            )
            self.turn = turn
            audio.onCapture = { [weak self] pcm, rms in
                turn.send(pcm)
                Task { @MainActor in self?.captured(rms: rms) }
            }
            turn.start(settings: settings)
            audio.startCapture()
            startSilenceTimer()
        }
    }

    private func stopListening() {
        guard phase == .listening else { return }
        silenceWatch?.cancel()
        audio.stopCapture()
        audio.onCapture = nil
        level = 0
        turn?.finishSending()
        phase = .thinking
        turns.insert(Turn(at: listenStarted, spoke: Date().timeIntervalSince(listenStarted), outcome: "…"), at: 0)
        turns = Array(turns.prefix(20))
        haptic(.stop)
    }

    private func interrupt() {
        silenceWatch?.cancel()
        turn?.cancel()
        turn = nil
        audio.stopCapture()
        audio.onCapture = nil
        audio.interruptPlayback()
        updateLastTurn("Interrupted")
    }

    private func captured(rms: Float) {
        guard phase == .listening else { return }
        level = rms
        if rms >= speechRMS {
            heardSpeech = true
            lastSpeech = Date()
        }
    }

    private func startSilenceTimer() {
        silenceWatch?.cancel()
        silenceWatch = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 200_000_000)
                self?.checkSilence()
            }
        }
    }

    private func checkSilence() {
        guard phase == .listening else { return }
        let now = Date()
        if now.timeIntervalSince(listenStarted) > maxTurn
            || (handsFree && heardSpeech && now.timeIntervalSince(lastSpeech) > handsFreeSilence) {
            stopListening()
        }
    }

    private func received(_ data: Data) {
        guard phase == .thinking || phase == .speaking else { return }
        if phase == .thinking {
            phase = .speaking
            haptic(.click)
        }
        audio.play(data)
    }

    private func finished(_ outcome: TalkTurn.Outcome) {
        guard turn != nil else { return }  // an interrupted turn reporting late
        turn = nil
        silenceWatch?.cancel()
        switch outcome {
        case .replied:
            updateLastTurn("Replied")
            audio.endOfStream { [weak self] in
                guard let self, self.phase == .speaking, self.turn == nil else { return }
                self.audio.stop()
                self.phase = .idle
            }
        case .noReply:
            updateLastTurn("No reply")
            audio.stop()
            phase = .idle
            haptic(.retry)
        case .failed(let message):
            updateLastTurn(message)
            audio.stop()
            phase = .failed(message)
            haptic(.failure)
        }
    }

    private func updateLastTurn(_ outcome: String) {
        guard !turns.isEmpty, turns[0].outcome == "…" else { return }
        turns[0].outcome = outcome
    }

    // MARK: Settings

    func save(settings: BridgeSettings) {
        self.settings = settings
        settings.save()
        if case .failed = phase { phase = .idle }
    }

    func checkBridge() async -> String {
        guard let base = settings.baseURL else { return "Invalid URL" }
        var request = URLRequest(url: base.appendingPathComponent("v1/status"))
        request.setValue("Bearer \(settings.secret)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 10
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            switch (response as? HTTPURLResponse)?.statusCode {
            case 200:
                let status = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
                return (status?["paired"] as? Bool) == true ? "Connected" : "Mac not paired with Muse"
            case 401: return "Wrong secret"
            case let code: return "HTTP \(code ?? 0)"
            }
        } catch {
            return "Can't reach the Mac"
        }
    }

    // MARK: Haptics

    private enum Haptic { case start, stop, click, retry, failure }

    private func haptic(_ kind: Haptic) {
        #if os(watchOS)
        let type: WKHapticType
        switch kind {
        case .start: type = .start
        case .stop: type = .stop
        case .click: type = .click
        case .retry: type = .retry
        case .failure: type = .failure
        }
        WKInterfaceDevice.current().play(type)
        #endif
    }
}

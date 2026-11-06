import AVFoundation

/// Microphone in, speaker out, both as PCM16LE mono 24 kHz: the bridge's format.
///
/// One engine runs for the whole turn: the input tap converts the hardware
/// format down to 24 kHz Int16, and a player node plays the streamed reply,
/// with a short pre-buffer to ride out network jitter.
final class AudioPipeline {
    static let sampleRate = 24_000.0

    /// Captured audio, called on the audio thread.
    var onCapture: ((Data, _ rms: Float) -> Void)?

    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let wireFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: sampleRate,
                                           channels: 1, interleaved: true)!
    private let playFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate,
                                           channels: 1, interleaved: false)!
    private var converter: AVAudioConverter?
    private var capturing = false

    // Playback state; touched on the main thread only.
    private var carry: UInt8?
    private var preBuffer: [AVAudioPCMBuffer] = []
    private var preBufferedFrames: AVAudioFrameCount = 0
    private var scheduled = 0
    private var streamEnded = false
    private var onPlaybackDone: (() -> Void)?
    private let preBufferFrames = AVAudioFrameCount(sampleRate * 0.25)

    init() {
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: playFormat)
    }

    static func requestPermission() async -> Bool {
        #if os(watchOS) || os(iOS)
        await AVAudioApplication.requestRecordPermission()
        #else
        true
        #endif
    }

    func start() throws {
        #if os(watchOS) || os(iOS)
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .default)
        try session.setActive(true)
        #endif
        if !engine.isRunning {
            // Touch the input node before starting so the engine's graph includes
            // the microphone; adding it to a running engine aborts on watchOS.
            _ = engine.inputNode
            engine.prepare()
            try engine.start()
        }
    }

    func stop() {
        stopCapture()
        player.stop()
        engine.stop()
        resetPlayback()
        #if os(watchOS) || os(iOS)
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        #endif
    }

    // MARK: Capture

    func startCapture() {
        guard !capturing else { return }
        let input = engine.inputNode
        guard input.outputFormat(forBus: 0).sampleRate > 0 else { return }  // no microphone route
        converter = nil
        // A nil format taps in the node's own format, whatever the hardware
        // settled on; the converter follows the buffers' actual format.
        input.installTap(onBus: 0, bufferSize: 2048, format: nil) { [weak self] buffer, _ in
            guard let self else { return }
            if self.converter?.inputFormat != buffer.format {
                self.converter = AVAudioConverter(from: buffer.format, to: self.wireFormat)
            }
            guard let converter = self.converter else { return }
            let ratio = Self.sampleRate / buffer.format.sampleRate
            let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
            guard let out = AVAudioPCMBuffer(pcmFormat: self.wireFormat, frameCapacity: capacity) else { return }
            var supplied = false
            var error: NSError?
            converter.convert(to: out, error: &error) { _, status in
                if supplied {
                    status.pointee = .noDataNow
                    return nil
                }
                supplied = true
                status.pointee = .haveData
                return buffer
            }
            guard error == nil, out.frameLength > 0, let samples = out.int16ChannelData?[0] else { return }
            let count = Int(out.frameLength)
            var sum: Float = 0
            for i in 0..<count {
                let s = Float(samples[i]) / 32768
                sum += s * s
            }
            self.onCapture?(Data(bytes: samples, count: count * 2), (sum / Float(count)).squareRoot())
        }
        capturing = true
    }

    func stopCapture() {
        guard capturing else { return }
        engine.inputNode.removeTap(onBus: 0)
        capturing = false
    }

    // MARK: Playback (main thread)

    /// Queue streamed reply audio. Bytes may split anywhere, even mid-sample.
    func play(_ data: Data) {
        var bytes = [UInt8](data)
        if let carry {
            bytes.insert(carry, at: 0)
            self.carry = nil
        }
        if bytes.count % 2 == 1 {
            carry = bytes.removeLast()
        }
        let frames = bytes.count / 2
        guard frames > 0,
              let buffer = AVAudioPCMBuffer(pcmFormat: playFormat, frameCapacity: AVAudioFrameCount(frames)),
              let out = buffer.floatChannelData?[0] else { return }
        bytes.withUnsafeBytes { raw in
            for i in 0..<frames {
                let sample = Int16(littleEndian: raw.loadUnaligned(fromByteOffset: i * 2, as: Int16.self))
                out[i] = Float(sample) / 32768
            }
        }
        buffer.frameLength = AVAudioFrameCount(frames)
        if player.isPlaying {
            schedule(buffer)
        } else {
            preBuffer.append(buffer)
            preBufferedFrames += buffer.frameLength
            if preBufferedFrames >= preBufferFrames { flushPreBuffer() }
        }
    }

    /// The reply stream is complete; `done` runs once everything queued has played.
    func endOfStream(_ done: @escaping () -> Void) {
        streamEnded = true
        onPlaybackDone = done
        flushPreBuffer()
        finishIfDrained()
    }

    /// Stop talking immediately (the user interrupted).
    func interruptPlayback() {
        player.stop()
        resetPlayback()
    }

    private func flushPreBuffer() {
        guard !preBuffer.isEmpty else { return }
        preBuffer.forEach(schedule)
        preBuffer.removeAll()
        preBufferedFrames = 0
        if !player.isPlaying { player.play() }
    }

    private func schedule(_ buffer: AVAudioPCMBuffer) {
        scheduled += 1
        player.scheduleBuffer(buffer) { [weak self] in
            DispatchQueue.main.async {
                guard let self, self.scheduled > 0 else { return }
                self.scheduled -= 1
                self.finishIfDrained()
            }
        }
    }

    private func finishIfDrained() {
        guard streamEnded, scheduled == 0, preBuffer.isEmpty, let done = onPlaybackDone else { return }
        resetPlayback()
        done()
    }

    private func resetPlayback() {
        carry = nil
        preBuffer.removeAll()
        preBufferedFrames = 0
        scheduled = 0
        streamEnded = false
        onPlaybackDone = nil
    }
}

import AVFoundation

/// Records the default input device straight into a 16 kHz mono 16-bit WAV,
/// which is exactly what whisper wants.
final class Recorder {
    var onLevel: ((Float) -> Void)?

    private let engine = AVAudioEngine()
    private var file: AVAudioFile?
    private var converter: AVAudioConverter?
    private var url: URL?
    private(set) var isRecording = false

    /// AVFoundation's installTap precondition is `Tap() == nullptr`: installing a
    /// second tap on a live bus throws an ObjC exception, and Swift cannot catch
    /// those — the process dies on the spot (this is exactly how the app crashed
    /// on 2026-11-17). So the tap is tracked here and always removed before a new
    /// one is installed. Removal must never be attempted when no tap exists
    /// either, for the same reason.
    private var tapInstalled = false

    private let target = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                       sampleRate: 16_000,
                                       channels: 1,
                                       interleaved: true)!

    func start() throws -> URL {
        teardown()                      // 自愈：绝不把第二个 tap 装到同一个 bus 上
        let input = engine.inputNode
        let hardware = input.inputFormat(forBus: 0)
        guard hardware.sampleRate > 0, hardware.channelCount > 0 else {
            throw NSError(domain: "VoiceType", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "没有可用的麦克风"])
        }
        guard let conv = AVAudioConverter(from: hardware, to: target) else {
            throw NSError(domain: "VoiceType", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "音频格式转换失败"])
        }

        let out = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("voicetype-\(Int(Date().timeIntervalSince1970)).wav")
        let handle = try AVAudioFile(forWriting: out,
                                     settings: target.settings,
                                     commonFormat: .pcmFormatInt16,
                                     interleaved: true)

        converter = conv
        file = handle
        url = out

        input.installTap(onBus: 0, bufferSize: 2048, format: hardware) { [weak self] buffer, _ in
            self?.process(buffer)
        }
        tapInstalled = true
        engine.prepare()
        do {
            try engine.start()
        } catch {
            teardown()                  // 起不来也不能把 tap 留在节点上
            throw error
        }
        isRecording = true
        return out
    }

    @discardableResult
    func stop() -> URL? {
        let finished = url
        teardown()
        return finished
    }

    /// Idempotent: safe when nothing is running. The single place that touches
    /// the tap or the engine state.
    private func teardown() {
        if tapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            tapInstalled = false
        }
        if engine.isRunning { engine.stop() }
        isRecording = false
        file = nil            // AVAudioFile flushes on dealloc
        converter = nil
        url = nil
    }

    deinit { teardown() }

    private func process(_ buffer: AVAudioPCMBuffer) {
        if let channel = buffer.floatChannelData?[0] {
            let count = Int(buffer.frameLength)
            guard count > 0 else { return }
            var sum: Float = 0
            for i in 0..<count { sum += channel[i] * channel[i] }
            onLevel?(sqrt(sum / Float(count)))
        }

        guard let converter, let file else { return }
        let ratio = converter.outputFormat.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 128
        guard let converted = AVAudioPCMBuffer(pcmFormat: converter.outputFormat,
                                               frameCapacity: capacity) else { return }

        var error: NSError?
        var supplied = false
        let status = converter.convert(to: converted, error: &error) { _, outStatus in
            if supplied {
                outStatus.pointee = .noDataNow
                return nil
            }
            supplied = true
            outStatus.pointee = .haveData
            return buffer
        }
        if status == .haveData || status == .inputRanDry {
            try? file.write(from: converted)
        }
    }
}

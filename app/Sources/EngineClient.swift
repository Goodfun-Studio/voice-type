import Foundation
import Darwin

/// Talks to the Python transcription daemon over a unix socket using
/// newline-delimited JSON, and starts the daemon if it is not up yet.
final class EngineClient {
    private var config: Config
    private var process: Process?
    private let stateLock = NSLock()

    init(config: Config) { self.config = config }

    func setLanguage(_ code: String) {
        stateLock.lock(); config.language = code; stateLock.unlock()
    }

    func shutdown() {
        stateLock.lock()
        let running = process
        process = nil
        stateLock.unlock()
        if let running, running.isRunning { running.terminate() }
    }

    func ensureRunning() -> Bool {
        if ping() { return true }
        logLine("engine not reachable, starting \(config.python)")
        spawn()
        let deadline = Date().addingTimeInterval(90)
        while Date() < deadline {
            if ping() { return true }
            Thread.sleep(forTimeInterval: 0.6)
        }
        logLine("engine did not come up in time")
        return false
    }

    private func spawn() {
        guard !config.python.isEmpty, !config.engine.isEmpty else {
            logLine("config.json missing python/engine path")
            return
        }
        let daemon = Process()
        daemon.executableURL = URL(fileURLWithPath: config.python)
        daemon.arguments = [config.engine, "--serve"]
        var environment = ProcessInfo.processInfo.environment
        environment["VOICETYPE_SOCK"] = config.socket
        daemon.environment = environment

        if !FileManager.default.fileExists(atPath: Paths.log.path) {
            FileManager.default.createFile(atPath: Paths.log.path, contents: nil)
        }
        if let handle = FileHandle(forWritingAtPath: Paths.log.path) {
            handle.seekToEndOfFile()
            daemon.standardOutput = handle
            daemon.standardError = handle
        }
        do {
            try daemon.run()
            stateLock.lock(); process = daemon; stateLock.unlock()
        } catch {
            logLine("failed to spawn engine: \(error)")
        }
    }

    func ping() -> Bool {
        (try? request(["cmd": "ping"], timeout: 1.2)) != nil
    }

    // MARK: streaming

    /// Ask the engine to start transcribing a file that is still being written.
    /// Returns false when the engine does not support it, so the caller can fall
    /// back to the one-shot path instead of losing the recording.
    func streamStart(wav: String, language: String?) -> Bool {
        do {
            _ = try request(["cmd": "stream_start", "wav": wav, "language": language ?? "zh"],
                            timeout: 120)
            return true
        } catch {
            logLine("stream_start failed (\(error.localizedDescription)) — will transcribe in one shot")
            return false
        }
    }

    /// Non-blocking: only called while recording, so a slow answer must not stall us.
    func streamState() -> [String: Any]? {
        try? request(["cmd": "stream_state"], timeout: 2)
    }

    func streamFinish(language: String?) throws -> [String: Any] {
        try request(["cmd": "stream_finish", "language": language ?? "zh"], timeout: 120)
    }

    func streamCancel() {
        _ = try? request(["cmd": "stream_cancel"], timeout: 3)
    }

    func transcribe(wav: String, language: String?) throws -> [String: Any] {
        try request(["cmd": "transcribe", "wav": wav, "language": language ?? "auto"],
                    timeout: 180)
    }

    // MARK: socket plumbing

    private func request(_ payload: [String: Any], timeout: TimeInterval) throws -> [String: Any] {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw socketError("socket() failed") }
        defer { close(fd) }

        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let path = config.socket
        let bytes = Array(path.utf8CString)
        guard bytes.count <= MemoryLayout.size(ofValue: address.sun_path) else {
            throw socketError("socket path too long")
        }
        withUnsafeMutableBytes(of: &address.sun_path) { destination in
            bytes.withUnsafeBytes { source in
                guard let from = source.baseAddress, let to = destination.baseAddress else { return }
                to.copyMemory(from: from, byteCount: bytes.count)
            }
        }

        let connected = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard connected == 0 else { throw socketError("connect() failed: \(String(cString: strerror(errno)))") }

        var data = try JSONSerialization.data(withJSONObject: payload)
        data.append(0x0A)
        let wrote = data.withUnsafeBytes { write(fd, $0.baseAddress, data.count) }
        guard wrote == data.count else { throw socketError("short write") }

        var buffer = Data()
        var chunk = [UInt8](repeating: 0, count: 65536)
        let deadline = Date().addingTimeInterval(timeout)
        while true {
            if let newline = buffer.firstIndex(of: 0x0A) {
                buffer = buffer[..<newline]
                break
            }
            let remaining = deadline.timeIntervalSinceNow
            guard remaining > 0 else { throw socketError("timed out waiting for engine") }
            var descriptor = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            let ready = poll(&descriptor, 1, Int32(remaining * 1000))
            guard ready > 0 else { throw socketError("engine did not answer") }
            let read = read(fd, &chunk, chunk.count)
            guard read > 0 else { break }
            buffer.append(contentsOf: chunk[0..<read])
        }
        guard !buffer.isEmpty,
              let object = try? JSONSerialization.jsonObject(with: buffer) as? [String: Any] else {
            throw socketError("bad response from engine")
        }
        if let error = object["error"] as? String { throw socketError(error) }
        return object
    }

    private func socketError(_ message: String) -> NSError {
        NSError(domain: "VoiceType", code: 3, userInfo: [NSLocalizedDescriptionKey: message])
    }
}

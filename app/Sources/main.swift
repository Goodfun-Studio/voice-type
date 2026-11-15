import AppKit
import AVFoundation
import ApplicationServices
import Carbon.HIToolbox

// MARK: - Paths & config

enum Paths {
    static let support: URL = {
        let u = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/VoiceType", isDirectory: true)
        try? FileManager.default.createDirectory(at: u, withIntermediateDirectories: true)
        return u
    }()
    static var socket: String { support.appendingPathComponent("engine.sock").path }
    static var log: URL { support.appendingPathComponent("voicetype.log") }
    static var config: URL { support.appendingPathComponent("config.json") }
    static var history: URL {
        let u = support.appendingPathComponent("history", isDirectory: true)
        try? FileManager.default.createDirectory(at: u, withIntermediateDirectories: true)
        return u
    }
}

func logLine(_ s: String) {
    // Single writer only: launchd already redirects our stderr into this same
    // file, and two writers interleaving mid-line corrupted the log.
    let f = DateFormatter(); f.dateFormat = "HH:mm:ss"
    let text = "[\(f.string(from: Date()))] \(s)\n"
    let fd = open(Paths.log.path, O_WRONLY | O_CREAT | O_APPEND, 0o644)
    guard fd >= 0 else { return }
    defer { close(fd) }
    _ = text.withCString { write(fd, $0, strlen($0)) }
}

struct Config: Codable {
    var python: String
    var engine: String
    var socket: String
    var language: String   // "zh" | "en" | "auto"
    var sound: Bool

    static func load() -> Config {
        if let d = try? Data(contentsOf: Paths.config),
           let c = try? JSONDecoder().decode(Config.self, from: d) { return c }
        return Config(python: "", engine: "", socket: Paths.socket, language: "zh", sound: true)
    }

    func save() {
        let enc = JSONEncoder(); enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        if let d = try? enc.encode(self) { try? d.write(to: Paths.config) }
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate {
    enum State { case idle, recording, transcribing }

    private var statusItem: NSStatusItem!
    private let hotkey = FnHotkey()
    private let altHotkey = CarbonHotKey()
    private let recorder = Recorder()
    private let hud = HUD()
    private var engine: EngineClient!
    private var config = Config.load()

    private var state: State = .idle
    private var targetApp: NSRunningApplication?
    private var startedAt: Date?
    private let minimumSeconds: TimeInterval = 0.3
    private var engineReady = false
    private var trusted = AXIsProcessTrusted()
    private var trustTimer: Timer?

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.accessory)
        engine = EngineClient(config: config)
        hud.prepare()

        recorder.onLevel = { [weak self] level in
            DispatchQueue.main.async { self?.hud.pushLevel(level) }
        }

        hotkey.onDoubleTap = { [weak self] in
            DispatchQueue.main.async { self?.toggle() }
        }
        altHotkey.register(keyCode: UInt32(kVK_Space), modifiers: UInt32(optionKey))
        altHotkey.onFire = { [weak self] in
            DispatchQueue.main.async { self?.toggle() }
        }
        hotkey.start()

        buildMenu()
        requestPermissionsIfNeeded()
        refreshStatus()

        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            self.engineReady = self.engine.ensureRunning()
            DispatchQueue.main.async {
                self.refreshStatus()
                self.hud.toast(self.engineReady ? "就绪 · 双击 Fn 说话" : "引擎启动失败，见日志")
            }
        }
    }

    private func requestPermissionsIfNeeded() {
        if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                logLine("microphone permission granted: \(granted)")
            }
        }
        if !trusted {
            logLine("accessibility permission missing, opening settings pane")
            startTrustWatch()
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
                guard let self else { return }
                self.hud.toast("去打开 VoiceType 的辅助功能开关")
                self.openAccessibilityPane()
            }
        }
    }

    // MARK: menu

    private func buildMenu() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu()

        if !trusted {
            let warn = NSMenuItem(title: "⚠️ 需要「辅助功能」权限 · 点这里开启",
                                  action: #selector(openAccessibilityPane), keyEquivalent: "")
            warn.target = self
            menu.addItem(warn)
            menu.addItem(.separator())
        }

        let header = NSMenuItem(title: "Voice Type", action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)

        let hint = NSMenuItem(title: "双击 Fn 开始 / 结束（备选 ⌥空格）", action: nil, keyEquivalent: "")
        hint.isEnabled = false
        menu.addItem(hint)
        menu.addItem(.separator())

        let langMenu = NSMenu()
        for (title, code) in [("中文", "zh"), ("English", "en"), ("自动识别", "auto")] {
            let item = NSMenuItem(title: title, action: #selector(setLanguage(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = code
            item.state = (config.language == code) ? .on : .off
            langMenu.addItem(item)
        }
        let langItem = NSMenuItem(title: "输入语言", action: nil, keyEquivalent: "")
        langItem.submenu = langMenu
        menu.addItem(langItem)

        addItem(menu, "检查辅助功能权限", #selector(grantAccessibility))
        addItem(menu, "检查麦克风权限", #selector(grantMicrophone))
        menu.addItem(.separator())
        addItem(menu, "重启转写引擎", #selector(restartEngine))
        addItem(menu, "打开日志", #selector(openLog))
        addItem(menu, "打开转写历史", #selector(openHistory))
        menu.addItem(.separator())
        addItem(menu, "退出", #selector(quit))

        statusItem.menu = menu
    }

    private func addItem(_ menu: NSMenu, _ title: String, _ action: Selector) {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        menu.addItem(item)
    }

    private func refreshStatus() {
        let symbol: String
        if !trusted {
            // Without Accessibility we can neither read Fn nor paste, so say so
            // in the menu bar instead of failing silently.
            symbol = "exclamationmark.triangle.fill"
        } else {
            switch state {
            case .idle: symbol = engineReady ? "mic" : "mic.slash"
            case .recording: symbol = "mic.fill"
            case .transcribing: symbol = "ellipsis.circle"
            }
        }
        statusItem.button?.image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Voice Type")
    }

    // MARK: flow

    private func toggle() {
        switch state {
        case .idle:
            startRecording()
        case .recording:
            finishRecording()
        case .transcribing:
            NSSound(named: "Basso")?.play()
        }
    }

    private func startRecording() {
        guard engineReady else {
            hud.toast("引擎还没起来")
            return
        }
        targetApp = NSWorkspace.shared.frontmostApplication
        do {
            _ = try recorder.start()
        } catch {
            logLine("record failed: \(error)")
            hud.toast("录音启动失败：麦克风权限？")
            NSApp.activate(ignoringOtherApps: true)
            grantMicrophone()
            return
        }
        startedAt = Date()
        state = .recording
        hud.showListening()
        refreshStatus()
        if config.sound { NSSound(named: "Tink")?.play() }
    }

    private func finishRecording() {
        guard let wav = recorder.stop() else { return }
        let seconds = Date().timeIntervalSince(startedAt ?? Date())
        state = .transcribing
        refreshStatus()
        if config.sound { NSSound(named: "Pop")?.play() }

        guard seconds >= minimumSeconds else {
            hud.hideSoon()
            state = .idle
            refreshStatus()
            return
        }
        hud.showTranscribing()

        let language = config.language == "auto" ? nil : config.language
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                let result = try self.engine.transcribe(wav: wav.path, language: language)
                let text = (result["text"] as? String) ?? ""
                let ms = (result["elapsedMs"] as? Int) ?? 0
                DispatchQueue.main.async {
                    self.deliver(text, ms: ms)
                    self.state = .idle
                    self.refreshStatus()
                }
            } catch {
                logLine("transcribe failed: \(error)")
                DispatchQueue.main.async {
                    self.hud.toast("转写失败")
                    self.state = .idle
                    self.refreshStatus()
                }
            }
        }
    }

    private func deliver(_ text: String, ms: Int) {
        guard !text.isEmpty else {
            hud.toast("没听到内容")
            NSSound(named: "Basso")?.play()
            return
        }
        writeHistory(text)
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)

        if let app = targetApp, !app.isActive {
            app.activate()
            usleep(140_000)
        }

        let trusted = AXIsProcessTrusted()
        if trusted {
            let source = CGEventSource(stateID: .combinedSessionState)
            let down = CGEvent(keyboardEventSource: source, virtualKey: 0x09, keyDown: true)
            let up = CGEvent(keyboardEventSource: source, virtualKey: 0x09, keyDown: false)
            down?.flags = .maskCommand
            up?.flags = .maskCommand
            down?.post(tap: .cghidEventTap)
            usleep(15_000)
            up?.post(tap: .cghidEventTap)
            hud.toast(String(text.prefix(40)))
        } else {
            logLine("no accessibility permission, text left on clipboard")
            hud.toast("已复制到剪贴板（缺辅助功能权限）")
        }
        logLine("done \(ms)ms: \(text)")
    }

    private func writeHistory(_ text: String) {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"
        let t = DateFormatter(); t.dateFormat = "HH:mm:ss"
        let file = Paths.history.appendingPathComponent("\(f.string(from: Date())).md")
        let line = "- \(t.string(from: Date()))  \(text)\n"
        if let h = try? FileHandle(forWritingTo: file) {
            h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); try? h.close()
        } else {
            try? line.write(to: file, atomically: true, encoding: .utf8)
        }
    }

    // MARK: actions

    @objc private func setLanguage(_ sender: NSMenuItem) {
        guard let code = sender.representedObject as? String else { return }
        config.language = code
        config.save()
        engine.setLanguage(code)
        buildMenu()
    }

    @objc private func grantAccessibility() { openAccessibilityPane() }

    @objc private func openAccessibilityPane() {
        // Asking also registers the app in the list, so the switch is right there.
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(opts)
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
            NSWorkspace.shared.open(url)
        }
    }

    private func startTrustWatch() {
        trustTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            guard let self else { return }
            let now = AXIsProcessTrusted()
            guard now != self.trusted else { return }
            self.trusted = now
            self.refreshStatus()
            self.buildMenu()
            self.hud.toast(now ? "辅助功能已就绪，现在能直接输入了" : "辅助功能权限被关掉了")
        }
    }

    @objc private func grantMicrophone() {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            hud.toast("麦克风权限已就绪")
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { ok in
                logLine("mic permission granted: \(ok)")
            }
        default:
            if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone") {
                NSWorkspace.shared.open(url)
            }
        }
    }

    @objc private func restartEngine() {
        engineReady = false
        refreshStatus()
        engine.shutdown()
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            self.engineReady = self.engine.ensureRunning()
            DispatchQueue.main.async {
                self.refreshStatus()
                self.hud.toast(self.engineReady ? "引擎已就绪" : "引擎启动失败")
            }
        }
    }

    @objc private func openLog() { NSWorkspace.shared.open(Paths.log) }
    @objc private func openHistory() { NSWorkspace.shared.open(Paths.history) }
    @objc private func quit() { NSApp.terminate(nil) }

    func applicationWillTerminate(_ note: Notification) {
        engine?.shutdown()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()

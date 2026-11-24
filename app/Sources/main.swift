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
    let f = DateFormatter(); f.dateFormat = "MM-dd HH:mm:ss"   // 必须有日期：两个写入者共用此文件，没有日期没法判断行属于哪一天
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
    /// nil means "never configured" — which is what triggers the first-run
    /// settings window.
    var trigger: Trigger?

    static func load() -> Config {
        if let d = try? Data(contentsOf: Paths.config),
           let c = try? JSONDecoder().decode(Config.self, from: d) { return c }
        return Config(python: "", engine: "", socket: Paths.socket,
                      language: "zh", sound: true, trigger: nil)
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
    private let trigger = TriggerController()
    private let settings = SettingsWindowController()
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
    private let cancelHotkey = CarbonHotKey()
    /// Bumped on every recording, so a late result from an abandoned take can
    /// never be pasted into whatever the user is doing now.
    private var generation = 0
    private var cancelledGeneration = -1
    private var trustTimer: Timer?

    private static let openSettingsNotification = Notification.Name("com.goodfunstudio.voicetype.openSettings")

    func applicationDidFinishLaunching(_ note: Notification) {
        // Two instances would both grab the trigger and both react to it.
        // So a second launch just asks the first one to show settings and exits.
        let bundleID = Bundle.main.bundleIdentifier ?? ""
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID)
            .filter { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }
        if let existing = others.first {
            logLine("second instance asked to open settings (existing pid \(existing.processIdentifier))")
            DistributedNotificationCenter.default().postNotificationName(
                Self.openSettingsNotification, object: nil, userInfo: nil, deliverImmediately: true)
            existing.activate()
            Thread.sleep(forTimeInterval: 0.4)
            NSApp.terminate(nil)
            return
        }
        DistributedNotificationCenter.default().addObserver(
            forName: Self.openSettingsNotification, object: nil, queue: .main) { [weak self] _ in
                self?.presentSettings(firstRun: false)
        }

        // A real Dock icon: it is the one entry point that is always findable,
        // and clicking it reopens the settings window (see
        // applicationShouldHandleReopen). A pure menu-bar agent was too easy to
        // lose — the icon can be pushed off the menu bar entirely.
        NSApp.setActivationPolicy(.regular)
        engine = EngineClient(config: config)
        hud.prepare()

        recorder.onLevel = { [weak self] level in
            DispatchQueue.main.async { self?.hud.pushLevel(level) }
        }

        // Only live while a recording is in flight — see beginRecordingWindow().
        cancelHotkey.onFire = { [weak self] in
            DispatchQueue.main.async { self?.cancel() }
        }
        trigger.onFire = { [weak self] in
            DispatchQueue.main.async { self?.toggle() }
        }
        // Start on the configured trigger (or the default) right away, so the
        // tool works even if the user closes the onboarding window.
        trigger.apply(config.trigger ?? .fallback)

        buildMenu()
        requestPermissionsIfNeeded()

        if config.trigger == nil {
            // Never configured: walk the user through picking a trigger rather
            // than silently assuming double-Fn.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
                self?.presentSettings(firstRun: true)
            }
        }
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

        let current = config.trigger ?? .fallback
        let hint = NSMenuItem(title: "触发方式：\(current.label)", action: nil, keyEquivalent: "")
        hint.isEnabled = false
        menu.addItem(hint)
        addItem(menu, "设置触发方式…", #selector(openSettings))
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

        addItem(menu, "取消当前录音 / 转写（Esc）", #selector(cancelCurrent))
        menu.addItem(.separator())
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
        guard let button = statusItem.button else { return }
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
        if let image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Voice Type") {
            button.image = image
            button.title = ""
        } else {
            // A status item whose image fails to resolve has zero width and is
            // simply invisible — which is how you end up with a tool that has no
            // way in. Never leave it blank.
            button.image = nil
            button.title = "🎙"
        }
    }

    // MARK: flow

    private func toggle() {
        switch state {
        case .idle:
            startRecording()
        case .recording:
            finishRecording()
        case .transcribing:
            cancel()
        }
    }

    private func beginRecordingWindow() {
        // Esc is registered only for the few seconds we are recording, so the
        // rest of the system keeps its Esc key untouched.
        if cancelHotkey.register(keyCode: UInt32(kVK_Escape), modifiers: 0) {
            logLine("take \(generation) started, esc armed")
        } else {
            logLine("esc hotkey unavailable, cancel from the menu instead")
        }
    }

    private func endRecordingWindow() {
        guard cancelHotkey.isRegistered else { return }
        cancelHotkey.unregister()
        logLine("esc released")
    }

    private func discard(_ url: URL?) {
        guard let url else { return }
        try? FileManager.default.removeItem(at: url)
    }

    private func startRecording() {
        // Never open a second recording on top of a live one: the extra tap
        // would be installed on a busy bus and AVFoundation would abort the
        // process. The state machine should make this impossible — this is the
        // belt-and-braces check.
        guard state == .idle else {
            logLine("start ignored: state=\(state)")
            return
        }
        guard engineReady else {
            hud.toast("引擎还没起来")
            return
        }
        targetApp = NSWorkspace.shared.frontmostApplication
        let wavURL: URL?
        do {
            wavURL = try recorder.start()
        } catch {
            logLine("record failed: \(error)")
            hud.toast("录音启动失败：麦克风权限？")
            NSApp.activate(ignoringOtherApps: true)
            grantMicrophone()
            return
        }
        generation += 1
        startedAt = Date()
        state = .recording
        beginRecordingWindow()
        hud.showListening()

        // 一次性转写：不做流式（2026-11-24 回滚）。实测真实使用环境下流式反而退化成
        // 20-35 秒尾延迟 + 碎片化输出 —— 见 README「流式为什么被停用」。
        refreshStatus()
        if config.sound { NSSound(named: "Tink")?.play() }
    }

    private func finishRecording() {
        guard state == .recording else { return }
        endRecordingWindow()
        guard let wav = recorder.stop() else { return }

        let seconds = Date().timeIntervalSince(startedAt ?? Date())
        state = .transcribing
        refreshStatus()
        if config.sound { NSSound(named: "Pop")?.play() }

        guard seconds >= minimumSeconds else {
            logLine("take too short (\(String(format: "%.2f", seconds))s), discarded")
            discard(wav)
            hud.hideSoon()
            state = .idle
            refreshStatus()
            return
        }
        hud.showTranscribing()

        let myGeneration = generation
        let language = config.language == "auto" ? nil : config.language
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            var text = ""
            var elapsed = 0
            do {
                let result = try self.engine.transcribe(wav: wav.path, language: language)
                text = (result["text"] as? String) ?? ""
                elapsed = (result["elapsedMs"] as? Int) ?? 0
            } catch {
                logLine("transcribe failed: \(error)")
            }

            DispatchQueue.main.async {
                self.discard(wav)
                // A take that is no longer current must not touch shared state.
                // This is the bug that killed the app: a cancelled take's late
                // result flipped state to .idle while a *newer* take was still
                // recording, and the next trigger then started a second
                // recording on top of the live one.
                guard myGeneration == self.generation else {
                    logLine("stale result of take \(myGeneration) ignored (current \(self.generation)): \(text.prefix(30))")
                    return
                }
                if self.cancelledGeneration == myGeneration {
                    logLine("result of take \(myGeneration) discarded after cancel: \(text.prefix(30))")
                } else if text.isEmpty {
                    self.hud.toast("没听到内容")
                    NSSound(named: "Basso")?.play()
                } else {
                    self.deliver(text, ms: elapsed)
                }
                self.state = .idle
                self.refreshStatus()
            }
        }
    }

    /// Esc (or a second double-tap) backs out: the audio is thrown away and
    /// nothing is transcribed or pasted.
    private func cancel() {
        switch state {
        case .recording:
            cancelledGeneration = generation
            endRecordingWindow()
            discard(recorder.stop())
            state = .idle
            refreshStatus()
            hud.toast("已取消")
            if config.sound { NSSound(named: "Basso")?.play() }
            logLine("take \(generation) cancelled, audio discarded")
        case .transcribing:
            cancelledGeneration = generation
            state = .idle
            refreshStatus()
            hud.toast("已取消")
            logLine("transcription of take \(generation) cancelled")
        case .idle:
            break
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

    @objc private func openSettings() { presentSettings(firstRun: false) }

    private func presentSettings(firstRun: Bool) {
        guard !settings.isVisible else { return }
        // While a real window is up, behave like a normal app: a Dock icon means
        // the window can be found (and Cmd-Tab'd to) instead of being lost.
        NSApp.setActivationPolicy(.regular)
        // Suspend the live trigger while the window is open, otherwise testing a
        // shortcut would start a real recording behind the settings window.
        trigger.stop()
        settings.onClose = { [weak self] in
            guard let self else { return }
            self.trigger.apply(self.config.trigger ?? .fallback)
            self.buildMenu()
        }
        settings.show(firstRun: firstRun, current: config.trigger ?? .fallback) { [weak self] chosen in
            guard let self else { return }
            logLine("settings onSave fired with \(chosen.label)")
            self.config.trigger = chosen
            self.config.save()
            self.trigger.apply(chosen)
            self.buildMenu()
            self.hud.toast("触发方式已设为「\(chosen.label)」")
            logLine("trigger set to \(chosen.label) (kind=\(chosen.kind.rawValue) keyCode=\(chosen.keyCode) taps=\(chosen.taps))")
        }
    }

    @objc private func cancelCurrent() { cancel() }

    @objc private func grantAccessibility() { openAccessibilityPane() }

    @objc private func openAccessibilityPane() {
        // The system prompt only appears for an app that is allowed to come
        // forward. As a background agent we are not frontmost, so without this
        // the prompt is silently swallowed and the user sees nothing.
        NSApp.activate(ignoringOtherApps: true)
        // Asking also registers the app in the list, so the switch is right there.
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(opts)
        // The legacy "com.apple.preference.security" id lands on the General pane
        // on current macOS; the PrivacySecurity extension id opens the real page.
        if let url = URL(string:
            "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility") {
            NSWorkspace.shared.open(url)
        }
    }

    private func startTrustWatch() {
        trustTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            guard let self else { return }
            let now = AXIsProcessTrusted()
            guard now != self.trusted else { return }
            self.trusted = now
            logLine(now ? "accessibility permission GRANTED — pasting directly now"
                        : "accessibility permission REMOVED")
            // Losing or gaining the permission changes what the Fn trigger can do.
            self.trigger.apply(self.config.trigger ?? .fallback)
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

    /// Reached when the bundle is opened again from Finder / Dock.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        presentSettings(firstRun: false)
        return true
    }

    func applicationWillTerminate(_ note: Notification) {
        endRecordingWindow()
        trigger.stop()
        engine?.shutdown()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()

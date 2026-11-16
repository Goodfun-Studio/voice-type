import AppKit

/// The settings / onboarding window: pick how Voice Type is triggered.
///
/// The important part is the "试试看" box. Picking a shortcut from a list and
/// hoping it works is a bad experience — especially for a key like Fn, whose
/// behaviour depends on what the rest of macOS has been told to do with it. So
/// whatever is selected is listened to live, and the box says so as soon as it
/// fires.
final class SettingsWindowController: NSObject, NSWindowDelegate {
    var onSave: ((Trigger) -> Void)?
    var onClose: (() -> Void)?

    private var window: NSWindow?
    private var isFirstRun = false
    private var presets = Trigger.presets
    private var selectedIndex = 0
    private var customTrigger: Trigger?

    private var radios: [NSButton] = []
    private var customRadio: NSButton!
    private var customLabel: NSTextField!
    private var recordButton: NSButton!
    private var statusLabel: NSTextField!
    private var saveButton: NSButton!
    private var cancelButton: NSButton!

    private var testController: TriggerController?
    private var recorder: ShortcutRecorder?
    private var fireCount = 0
    /// AppKit also sends a radio button's action when we set its state in code.
    /// Without this gate our own refresh looks exactly like a user click, and
    /// the selection silently walks down the list.
    private var isProgrammaticUpdate = false
    private var isFinishing = false

    var isVisible: Bool { window != nil }

    // MARK: - show

    func show(firstRun: Bool, current: Trigger, onSave: @escaping (Trigger) -> Void) {
        self.isFirstRun = firstRun
        self.onSave = onSave
        if let index = presets.firstIndex(of: current) {
            selectedIndex = index
            customTrigger = nil
        } else {
            selectedIndex = presets.count
            customTrigger = current
        }
        isFinishing = false
        buildWindow()
        refreshSelectionUI()
        startTestListener()
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
        window?.center()
    }

    private func buildWindow() {
        let size = NSSize(width: 470, height: isFirstRun ? 470 : 430)
        let panel = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                             styleMask: [.titled, .closable],
                             backing: .buffered,
                             defer: false)
        panel.title = isFirstRun ? "Voice Type · 先设置一下" : "Voice Type 设置"
        panel.isReleasedWhenClosed = false
        panel.delegate = self
        window = panel

        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 14
        root.edgeInsets = NSEdgeInsets(top: 20, left: 22, bottom: 20, right: 22)
        root.translatesAutoresizingMaskIntoConstraints = false
        panel.contentView = NSView()
        panel.contentView?.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: panel.contentView!.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: panel.contentView!.trailingAnchor),
            root.topAnchor.constraint(equalTo: panel.contentView!.topAnchor),
            root.bottomAnchor.constraint(lessThanOrEqualTo: panel.contentView!.bottomAnchor),
        ])

        let lead = NSTextField(wrappingLabelWithString: isFirstRun
            ? "选一个你顺手的触发方式：按住它开始说话，说完再按一次结束。\n设置里随时能改。"
            : "按住它开始说话，说完再按一次结束。")
        lead.font = .systemFont(ofSize: 12)
        lead.textColor = .secondaryLabelColor
        lead.preferredMaxLayoutWidth = 420
        root.addArrangedSubview(lead)

        root.addArrangedSubview(sectionLabel("触发方式"))

        for (index, preset) in presets.enumerated() {
            let button = NSButton(radioButtonWithTitle: preset.label,
                                  target: self, action: #selector(radioChanged(_:)))
            button.tag = index
            button.state = index == selectedIndex ? .on : .off
            radios.append(button)
            root.addArrangedSubview(indented(button))
        }

        // Custom row: radio + current value + record button
        let customRow = NSStackView()
        customRow.orientation = .horizontal
        customRow.spacing = 8
        customRadio = NSButton(radioButtonWithTitle: "自定义", target: self, action: #selector(radioChanged(_:)))
        customRadio.tag = presets.count
        customRow.addArrangedSubview(customRadio)
        customLabel = NSTextField(labelWithString: customTrigger?.label ?? "尚未录制")
        customLabel.textColor = .secondaryLabelColor
        customLabel.font = .monospacedSystemFont(ofSize: 12, weight: .medium)
        customRow.addArrangedSubview(customLabel)
        recordButton = NSButton(title: "录制…", target: self, action: #selector(recordTapped))
        recordButton.bezelStyle = .rounded
        recordButton.controlSize = .small
        customRow.addArrangedSubview(recordButton)
        root.addArrangedSubview(customRow)

        // Test area
        root.addArrangedSubview(sectionLabel("试试看"))
        let testBox = NSBox()
        testBox.boxType = .custom
        testBox.borderWidth = 1
        testBox.borderColor = .separatorColor
        testBox.cornerRadius = 8
        testBox.fillColor = NSColor.controlBackgroundColor
        testBox.translatesAutoresizingMaskIntoConstraints = false
        statusLabel = NSTextField(labelWithString: "")
        statusLabel.font = .systemFont(ofSize: 13, weight: .medium)
        statusLabel.translatesAutoresizingMaskIntoConstraints = false
        testBox.addSubview(statusLabel)
        NSLayoutConstraint.activate([
            testBox.widthAnchor.constraint(equalToConstant: 426),
            testBox.heightAnchor.constraint(equalToConstant: 46),
            statusLabel.centerXAnchor.constraint(equalTo: testBox.centerXAnchor),
            statusLabel.centerYAnchor.constraint(equalTo: testBox.centerYAnchor),
        ])
        root.addArrangedSubview(testBox)

        let note = NSTextField(wrappingLabelWithString:
            "触发键本身不需要任何权限。但要让文字直接敲进光标，还需要在「辅助功能」里打开 Voice Type —— 菜单栏图标变 ⚠️ 时点它可直达设置页。")
        note.font = .systemFont(ofSize: 11)
        note.textColor = .tertiaryLabelColor
        note.preferredMaxLayoutWidth = 426
        root.addArrangedSubview(note)

        // Buttons
        let buttons = NSStackView()
        buttons.orientation = .horizontal
        buttons.spacing = 10
        buttons.translatesAutoresizingMaskIntoConstraints = false
        cancelButton = NSButton(title: isFirstRun ? "用默认（连按两次 Fn）" : "取消",
                                target: self, action: #selector(cancelTapped))
        cancelButton.bezelStyle = .rounded
        saveButton = NSButton(title: isFirstRun ? "开始使用" : "保存",
                              target: self, action: #selector(saveTapped))
        saveButton.bezelStyle = .rounded
        buttons.addArrangedSubview(cancelButton)
        buttons.addArrangedSubview(saveButton)
        let spacer = NSView()
        spacer.translatesAutoresizingMaskIntoConstraints = false
        let row = NSStackView(views: [spacer, buttons])
        row.orientation = .horizontal
        row.translatesAutoresizingMaskIntoConstraints = false
        row.widthAnchor.constraint(equalToConstant: 426).isActive = true
        root.addArrangedSubview(row)
    }

    private func sectionLabel(_ text: String) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = .systemFont(ofSize: 12, weight: .semibold)
        return label
    }

    private func indented(_ view: NSView) -> NSView {
        let spacer = NSView(frame: NSRect(x: 0, y: 0, width: 14, height: 1))
        spacer.translatesAutoresizingMaskIntoConstraints = false
        spacer.widthAnchor.constraint(equalToConstant: 14).isActive = true
        let row = NSStackView(views: [spacer, view])
        row.orientation = .horizontal
        row.spacing = 0
        return row
    }

    // MARK: - selection

    private var currentTrigger: Trigger? {
        if selectedIndex < presets.count { return presets[selectedIndex] }
        return customTrigger
    }

    private func refreshSelectionUI() {
        isProgrammaticUpdate = true
        defer { isProgrammaticUpdate = false }
        for (index, button) in radios.enumerated() {
            button.state = index == selectedIndex ? .on : .off
        }
        customRadio.state = selectedIndex == presets.count ? .on : .off
        customLabel.stringValue = customTrigger?.label ?? "尚未录制"
        customLabel.textColor = customTrigger == nil ? .tertiaryLabelColor : .secondaryLabelColor
        saveButton.isEnabled = currentTrigger != nil
    }

    @objc private func radioChanged(_ sender: NSButton) {
        guard !isProgrammaticUpdate else { return }
        selectedIndex = sender.tag
        refreshSelectionUI()
        startTestListener()
    }

    // MARK: - test listener

    private func startTestListener() {
        testController?.stop()
        testController = nil
        fireCount = 0
        guard recorder == nil else { return }
        guard let trigger = currentTrigger else {
            setStatus("先录制一个快捷键，或从上面选一个", color: .secondaryLabelColor)
            return
        }
        let controller = TriggerController()
        controller.onFire = { [weak self] in self?.triggerFired() }
        if controller.apply(trigger) {
            testController = controller
            setStatus("● 等待中 · 现在按一下「\(trigger.label)」", color: .secondaryLabelColor)
        } else {
            setStatus("这个组合被系统占用了，换一个", color: .systemOrange)
        }
    }

    private func triggerFired() {
        fireCount += 1
        setStatus("✓ 检测到了 · 第 \(fireCount) 次", color: .systemGreen)
        // Fade back to the waiting state so a second press is visibly a second press.
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [weak self] in
            guard let self, self.testController != nil else { return }
            self.setStatus("● 等待中 · 再按一下试试", color: .secondaryLabelColor)
        }
    }

    private func setStatus(_ text: String, color: NSColor) {
        statusLabel.stringValue = text
        statusLabel.textColor = color
    }

    // MARK: - recording

    @objc private func recordTapped() {
        testController?.stop()
        testController = nil
        selectedIndex = presets.count
        refreshSelectionUI()
        recordButton.isEnabled = false
        setStatus("● 现在按下你想用的键或组合…", color: .systemBlue)

        let recorder = ShortcutRecorder()
        recorder.onOutcome = { [weak self] outcome in
            guard let self else { return }
            self.recorder = nil
            self.recordButton.isEnabled = true
            switch outcome {
            case .recorded(let trigger):
                self.customTrigger = trigger
                self.refreshSelectionUI()
                self.startTestListener()
            case .rejected(let reason):
                self.setStatus(reason, color: .systemOrange)
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { [weak self] in
                    self?.startTestListener()
                }
            }
        }
        self.recorder = recorder
        recorder.start()
    }

    // MARK: - buttons

    @objc private func saveTapped() {
        let trigger = currentTrigger ?? Trigger.fallback
        finish(with: trigger)
    }

    @objc private func cancelTapped() {
        finish(with: isFirstRun ? Trigger.fallback : nil)
    }

    private func finish(with trigger: Trigger?) {
        guard !isFinishing else { return }
        isFinishing = true
        stopEverything()
        // Detach before hiding: a button that outlives its window can still fire
        // its action, and would then write a trigger the user never chose.
        for button in radios { button.target = nil; button.action = nil }
        customRadio?.target = nil
        customRadio?.action = nil
        recordButton?.target = nil
        recordButton?.action = nil
        let handler = onSave
        window?.orderOut(nil)
        window = nil
        onClose = nil            // closing must not re-apply behind a save
        if let trigger { handler?(trigger) }
        isFinishing = false
    }

    func windowWillClose(_ notification: Notification) {
        guard !isFinishing else { return }
        isFinishing = true
        stopEverything()
        for button in radios { button.target = nil; button.action = nil }
        customRadio?.target = nil
        customRadio?.action = nil
        window = nil
        let close = onClose
        onClose = nil
        close?()
        isFinishing = false
    }

    private func stopEverything() {
        testController?.stop()
        testController = nil
        recorder?.stop()
        recorder = nil
    }
}

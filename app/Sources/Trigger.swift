import AppKit
import Carbon.HIToolbox

/// How the user asks Voice Type to start listening.
///
/// Two families, both of which work **without** Accessibility permission —
/// that matters, because the permission is a separate switch the user has to
/// find, and a trigger that does not work until then feels broken.
///
///   .modifierTap — tap a modifier key (Fn, right ⌘, right ⌥) once or twice
///   .keyCombo    — a key combination, registered as a Carbon hot key
struct Trigger: Codable, Equatable {
    enum Kind: String, Codable {
        case modifierTap
        case keyCombo
    }

    var kind: Kind
    /// modifierTap: which modifier key. keyCombo: the virtual key code.
    var keyCode: Int
    /// modifierTap: 1 or 2 taps. Unused for combos.
    var taps: Int
    /// keyCombo: Carbon modifier mask.
    var modifiers: Int
    /// What the menu shows the user.
    var label: String

    // MARK: presets

    static let doubleFn = Trigger(kind: .modifierTap, keyCode: kVK_Function,
                                  taps: 2, modifiers: 0, label: "连按两次 Fn")
    static let singleFn = Trigger(kind: .modifierTap, keyCode: kVK_Function,
                                  taps: 1, modifiers: 0, label: "按一次 Fn")
    static let doubleRightCommand = Trigger(kind: .modifierTap, keyCode: kVK_RightCommand,
                                            taps: 2, modifiers: 0, label: "连按两次右 Command")
    static let doubleRightOption = Trigger(kind: .modifierTap, keyCode: kVK_RightOption,
                                           taps: 2, modifiers: 0, label: "连按两次右 Option")
    static let optionSpace = Trigger(kind: .keyCombo, keyCode: kVK_Space,
                                     taps: 1, modifiers: Int(optionKey), label: "⌥ + 空格")

    static let presets: [Trigger] = [
        .doubleFn, .singleFn, .doubleRightCommand, .doubleRightOption, .optionSpace,
    ]

    static var fallback: Trigger { .doubleFn }

    // MARK: helpers

    /// The modifier flag that this key code sets, so a flagsChanged event can be
    /// read as "pressed" / "released".
    static func modifierMask(for keyCode: Int) -> NSEvent.ModifierFlags? {
        switch keyCode {
        case kVK_Function: return .function
        case kVK_Command, kVK_RightCommand: return .command
        case kVK_Option, kVK_RightOption: return .option
        case kVK_Control, kVK_RightControl: return .control
        case kVK_Shift, kVK_RightShift: return .shift
        default: return nil
        }
    }

    static func carbonMask(from flags: NSEvent.ModifierFlags) -> Int {
        var mask = 0
        if flags.contains(.command) { mask |= Int(cmdKey) }
        if flags.contains(.option) { mask |= Int(optionKey) }
        if flags.contains(.control) { mask |= Int(controlKey) }
        if flags.contains(.shift) { mask |= Int(shiftKey) }
        return mask
    }

    /// A combo has to keep at least one of ⌘ ⌥ ⌃. Without this, recording "v"
    /// would register a global hot key that swallows every "v" the user types.
    static func isUsableCombo(keyCode: Int, flags: NSEvent.ModifierFlags) -> Bool {
        if flags.contains(.command) || flags.contains(.option) || flags.contains(.control) {
            return true
        }
        // Function keys are fine on their own.
        let functionKeys: Set<Int> = [kVK_F1, kVK_F2, kVK_F3, kVK_F4, kVK_F5, kVK_F6,
                                      kVK_F7, kVK_F8, kVK_F9, kVK_F10, kVK_F11, kVK_F12]
        return functionKeys.contains(keyCode)
    }

    var isUsable: Bool {
        switch kind {
        case .modifierTap: return Trigger.modifierMask(for: keyCode) != nil
        case .keyCombo: return codes(for: modifiers).count > 0 || modifiers > 0
        }
    }

    /// Renders a Carbon modifier mask back into something readable.
    static func describe(keyCode: Int, modifiers: Int) -> String {
        var parts: [String] = []
        if modifiers & Int(controlKey) != 0 { parts.append("⌃") }
        if modifiers & Int(optionKey) != 0 { parts.append("⌥") }
        if modifiers & Int(shiftKey) != 0 { parts.append("⇧") }
        if modifiers & Int(cmdKey) != 0 { parts.append("⌘") }
        parts.append(keyName(for: keyCode))
        return parts.joined()
    }

    static func keyName(for keyCode: Int) -> String {
        let map: [Int: String] = [
            kVK_Space: "空格", kVK_Return: "↩", kVK_Tab: "⇥", kVK_Escape: "⎋",
            kVK_Delete: "⌫", kVK_ForwardDelete: "⌦", kVK_LeftArrow: "←",
            kVK_RightArrow: "→", kVK_UpArrow: "↑", kVK_DownArrow: "↓",
            kVK_F1: "F1", kVK_F2: "F2", kVK_F3: "F3", kVK_F4: "F4", kVK_F5: "F5",
            kVK_F6: "F6", kVK_F7: "F7", kVK_F8: "F8", kVK_F9: "F9", kVK_F10: "F10",
            kVK_F11: "F11", kVK_F12: "F12",
        ]
        if let named = map[keyCode] { return named }
        // Fall back to the character the key produces.
        if let source = TISCopyCurrentKeyboardLayoutInputSource()?.takeRetainedValue(),
           let layout = TISGetInputSourceProperty(source, kTISPropertyUnicodeKeyLayoutData) {
            let data = Unmanaged<CFData>.fromOpaque(layout).takeUnretainedValue() as Data
            var deadKeys: UInt32 = 0
            var length = 0
            var chars = [UniChar](repeating: 0, count: 4)
            let status = data.withUnsafeBytes { raw -> OSStatus in
                guard let layoutPtr = raw.bindMemory(to: UCKeyboardLayout.self).baseAddress else {
                    return -1
                }
                return UCKeyTranslate(layoutPtr, UInt16(keyCode), UInt16(kUCKeyActionDisplay),
                                      0, UInt32(LMGetKbdType()), OptionBits(kUCKeyTranslateNoDeadKeysBit),
                                      &deadKeys, chars.count, &length, &chars)
            }
            if status == noErr, length > 0 {
                return String(utf16CodeUnits: chars, count: length).uppercased()
            }
        }
        return "键码 \(keyCode)"
    }
}

private func codes(for modifiers: Int) -> [Int] { modifiers == 0 ? [] : [modifiers] }

// MARK: - listening

/// Watches one modifier key and reports a tap (or a double tap).
///
/// Some care is needed so that ordinary shortcuts do not fire it: holding Fn
/// and pressing an arrow key is *not* a tap, so any key pressed while the
/// modifier is down marks that press as disturbed.
final class ModifierTapWatcher {
    var onTrigger: (() -> Void)?

    private let keyCode: Int
    private let requiredTaps: Int
    private var monitors: [Any] = []

    private var isDown = false
    private var disturbed = false
    private var pressedAt = Date.distantPast
    private var lastTapAt = Date.distantPast

    private let maxTapLength: TimeInterval = 0.40
    private let doubleTapWindow: TimeInterval = 0.45

    init(keyCode: Int, taps: Int) {
        self.keyCode = keyCode
        self.requiredTaps = max(1, taps)
    }

    func start() {
        stop()
        let handler: (NSEvent) -> Void = { [weak self] event in self?.handle(event) }
        monitors.append(NSEvent.addGlobalMonitorForEvents(matching: [.flagsChanged, .keyDown]) { handler($0) })
        monitors.append(NSEvent.addLocalMonitorForEvents(matching: [.flagsChanged, .keyDown]) {
            handler($0)
            return $0
        })
    }

    func stop() {
        for monitor in monitors { NSEvent.removeMonitor(monitor) }
        monitors.removeAll()
        isDown = false
        disturbed = false
        lastTapAt = .distantPast
    }

    private func handle(_ event: NSEvent) {
        if event.type == .keyDown {
            // Another key went down before this modifier came up: it is a
            // shortcut (Fn+←, ⌘C…), not a tap.
            if isDown { disturbed = true }
            return
        }
        guard event.keyCode == keyCode,
              let mask = Trigger.modifierMask(for: keyCode) else { return }

        let down = event.modifierFlags.contains(mask)
        if down && !isDown {
            isDown = true
            disturbed = false
            pressedAt = Date()
            return
        }
        guard !down && isDown else { return }
        isDown = false

        guard !disturbed, Date().timeIntervalSince(pressedAt) <= maxTapLength else {
            lastTapAt = .distantPast
            return
        }

        if requiredTaps == 1 {
            fire()
            return
        }
        let now = Date()
        if now.timeIntervalSince(lastTapAt) <= doubleTapWindow {
            lastTapAt = .distantPast
            fire()
        } else {
            lastTapAt = now
        }
    }

    private func fire() {
        DispatchQueue.main.async { [weak self] in self?.onTrigger?() }
    }
}

/// Turns a `Trigger` into a live listener. One place for both families.
final class TriggerController {
    var onFire: (() -> Void)?

    private var modifierWatcher: ModifierTapWatcher?
    private var comboHotKey: CarbonHotKey?
    private(set) var trigger: Trigger = .fallback

    var isListening: Bool { modifierWatcher != nil || comboHotKey != nil }

    @discardableResult
    func apply(_ trigger: Trigger) -> Bool {
        stop()
        self.trigger = trigger
        var ok = true
        logLine("trigger applied: \(trigger.label) [\(trigger.kind.rawValue) keyCode=\(trigger.keyCode) taps=\(trigger.taps)]")
        switch trigger.kind {
        case .modifierTap:
            // Note: an event tap that consumes Fn was implemented and measured
            // (2026-11-16): the tap did receive Fn and did start recordings, but
            // macOS still opened the emoji picker at the same millisecond. The
            // globe action happens below the CGEvent layer, so consuming the
            // event cannot stop it — while it *does* break Fn+arrow shortcuts.
            // Not worth the trade, so Fn stays on passive monitoring.
            let watcher = ModifierTapWatcher(keyCode: trigger.keyCode, taps: trigger.taps)
            watcher.onTrigger = { [weak self] in self?.onFire?() }
            watcher.start()
            modifierWatcher = watcher
        case .keyCombo:
            let hotKey = CarbonHotKey()
            hotKey.onFire = { [weak self] in self?.onFire?() }
            ok = hotKey.register(keyCode: UInt32(trigger.keyCode),
                                 modifiers: UInt32(trigger.modifiers))
            if ok {
                comboHotKey = hotKey
                logLine("trigger combo registered ok")
            } else {
                logLine("trigger combo refused by the system — pick another one")
            }
        }
        return ok
    }

    func stop() {
        modifierWatcher?.stop()
        modifierWatcher = nil
        comboHotKey?.unregister()
        comboHotKey = nil
    }
}

/// Records whatever the user presses next, for the "自定义" option.
final class ShortcutRecorder {
    enum Outcome {
        case recorded(Trigger)
        case rejected(String)
    }

    var onOutcome: ((Outcome) -> Void)?

    private var monitors: [Any] = []
    private var modifierDownAt: Date?
    private var disturbed = false

    func start() {
        stop()
        let handler: (NSEvent) -> Void = { [weak self] event in self?.handle(event) }
        monitors.append(NSEvent.addGlobalMonitorForEvents(matching: [.flagsChanged, .keyDown]) { handler($0) })
        monitors.append(NSEvent.addLocalMonitorForEvents(matching: [.flagsChanged, .keyDown]) {
            handler($0)
            return $0
        })
    }

    func stop() {
        for monitor in monitors { NSEvent.removeMonitor(monitor) }
        monitors.removeAll()
        modifierDownAt = nil
        disturbed = false
    }

    private func handle(_ event: NSEvent) {
        if event.type == .keyDown {
            if modifierDownAt != nil { disturbed = true }
            let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
            guard Trigger.isUsableCombo(keyCode: Int(event.keyCode), flags: flags) else {
                finish(.rejected("这个组合没带 ⌘/⌥/⌃，会被全局拦截，换一个吧"))
                return
            }
            let mask = Trigger.carbonMask(from: flags)
            let label = Trigger.describe(keyCode: Int(event.keyCode), modifiers: mask)
            finish(.recorded(Trigger(kind: .keyCombo, keyCode: Int(event.keyCode),
                                     taps: 1, modifiers: mask, label: label)))
            return
        }

        guard let mask = Trigger.modifierMask(for: Int(event.keyCode)) else { return }
        let down = event.modifierFlags.contains(mask)
        if down && modifierDownAt == nil {
            modifierDownAt = Date()
            disturbed = false
            return
        }
        guard !down, let pressedAt = modifierDownAt else { return }
        modifierDownAt = nil
        guard !disturbed, Date().timeIntervalSince(pressedAt) <= 0.40 else { return }

        // A lone modifier is only usable if it is not one the system already
        // owns for plain typing (⇧ / ⌃ alone would fight with real shortcuts).
        if mask == .shift {
            finish(.rejected("单独的 Shift 会和普通输入冲突，换一个吧"))
            return
        }
        let label = Self.modifierLabel(for: Int(event.keyCode))
        finish(.recorded(Trigger(kind: .modifierTap, keyCode: Int(event.keyCode),
                                 taps: 2, modifiers: 0, label: "连按两次\(label)")))
    }

    private func finish(_ outcome: Outcome) {
        stop()
        DispatchQueue.main.async { [weak self] in self?.onOutcome?(outcome) }
    }

    static func modifierLabel(for keyCode: Int) -> String {
        switch keyCode {
        case kVK_Function: return " Fn"
        case kVK_RightCommand: return "右 Command"
        case kVK_Command: return "Command"
        case kVK_RightOption: return "右 Option"
        case kVK_Option: return "Option"
        case kVK_RightControl: return "右 Control"
        case kVK_Control: return "Control"
        default: return "该键"
        }
    }
}

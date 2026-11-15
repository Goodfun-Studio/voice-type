import AppKit
import Carbon.HIToolbox

/// Double-tap of the Fn / globe key, detected from flagsChanged events.
/// Needs Accessibility permission for the global monitor; if it is missing the
/// monitor silently never fires and the Carbon fallback (⌥Space) still works.
final class FnHotkey {
    var onDoubleTap: (() -> Void)?

    private var globalMonitor: Any?
    private var localMonitor: Any?
    private var isDown = false
    private var pressedAt = Date.distantPast
    private var lastTapAt = Date.distantPast

    private let doubleTapWindow: TimeInterval = 0.45
    private let maxTapLength: TimeInterval = 0.5

    func start() {
        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged) { [weak self] event in
            self?.handle(event)
        }
        localMonitor = NSEvent.addLocalMonitorForEvents(matching: .flagsChanged) { [weak self] event in
            self?.handle(event)
            return event
        }
    }

    func stop() {
        if let m = globalMonitor { NSEvent.removeMonitor(m) }
        if let m = localMonitor { NSEvent.removeMonitor(m) }
        globalMonitor = nil
        localMonitor = nil
    }

    private func handle(_ event: NSEvent) {
        let now = Date()
        let fnDown = event.modifierFlags.contains(.function)

        if fnDown && !isDown {
            isDown = true
            pressedAt = now
            return
        }
        guard !fnDown && isDown else { return }
        isDown = false

        guard now.timeIntervalSince(pressedAt) <= maxTapLength else { return }
        if now.timeIntervalSince(lastTapAt) <= doubleTapWindow {
            lastTapAt = .distantPast
            onDoubleTap?()
        } else {
            lastTapAt = now
        }
    }
}

/// Carbon hot key — no Accessibility permission required, used as a fallback.
final class CarbonHotKey {
    static var current: CarbonHotKey?
    var onFire: (() -> Void)?

    private var hotKeyRef: EventHotKeyRef?
    private var handlerRef: EventHandlerRef?

    init() { CarbonHotKey.current = self }

    func register(keyCode: UInt32, modifiers: UInt32) {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, _ -> OSStatus in
            CarbonHotKey.current?.onFire?()
            return noErr
        }, 1, &spec, nil, &handlerRef)

        let hotKeyID = EventHotKeyID(signature: OSType(0x5654_5950), id: 1)
        RegisterEventHotKey(keyCode, modifiers, hotKeyID, GetApplicationEventTarget(), 0, &hotKeyRef)
    }
}

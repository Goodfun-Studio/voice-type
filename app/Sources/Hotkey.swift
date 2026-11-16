import AppKit
import Carbon.HIToolbox

/// Carbon hot keys. No Accessibility permission required, so a cancel key can
/// work before the user has granted anything.
///
/// Events are dispatched by hot key ID through a static registry: with more than
/// one hot key alive, a single shared "current instance" would cross the wires
/// (pressing ⌥Space would fire Esc's handler).
final class CarbonHotKey {
    private static var registry: [UInt32: CarbonHotKey] = [:]
    private static var nextID: UInt32 = 1
    private static var handlerInstalled = false

    var onFire: (() -> Void)?

    private var ref: EventHotKeyRef?
    private var id: UInt32 = 0
    private var registeredKeyCode: UInt32?
    private var registeredModifiers: UInt32?

    private static func installHandlerOnce() {
        guard !handlerInstalled else { return }
        handlerInstalled = true
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        var handler: EventHandlerRef?
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ -> OSStatus in
            var hotKeyID = EventHotKeyID()
            let err = GetEventParameter(event,
                                        EventParamName(kEventParamDirectObject),
                                        EventParamType(typeEventHotKeyID),
                                        nil,
                                        MemoryLayout<EventHotKeyID>.size,
                                        nil,
                                        &hotKeyID)
            guard err == noErr, let target = CarbonHotKey.registry[hotKeyID.id] else { return noErr }
            target.onFire?()
            return noErr
        }, 1, &spec, nil, &handler)
    }

    /// Registers the hot key, replacing any registration this instance already
    /// holds. Returns false if the system refused it.
    @discardableResult
    func register(keyCode: UInt32, modifiers: UInt32) -> Bool {
        CarbonHotKey.installHandlerOnce()
        unregister()

        CarbonHotKey.nextID += 1
        let newID = CarbonHotKey.nextID
        let hotKeyID = EventHotKeyID(signature: OSType(0x5654_5950), id: newID)
        var hotKeyRef: EventHotKeyRef?
        let status = RegisterEventHotKey(keyCode, modifiers, hotKeyID,
                                         GetApplicationEventTarget(), 0, &hotKeyRef)
        guard status == noErr, let hotKeyRef else {
            logLine("RegisterEventHotKey keyCode=\(keyCode) failed: \(status)")
            return false
        }
        ref = hotKeyRef
        id = newID
        registeredKeyCode = keyCode
        registeredModifiers = modifiers
        CarbonHotKey.registry[newID] = self
        return true
    }

    /// Gives the key back to the rest of the system.
    func unregister() {
        if let ref { UnregisterEventHotKey(ref) }
        if id != 0 { CarbonHotKey.registry.removeValue(forKey: id) }
        ref = nil
        id = 0
        registeredKeyCode = nil
        registeredModifiers = nil
    }

    var isRegistered: Bool { ref != nil }
}

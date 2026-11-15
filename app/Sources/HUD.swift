import AppKit

/// Floating pill at the bottom of the screen. Non-activating, so the app you
/// are typing into never loses focus.
final class HUD {
    private var panel: NSPanel!
    private var view: HUDView!
    private var ticker: Timer?
    private var dismiss: DispatchWorkItem?
    private var lastLevelAt = Date.distantPast

    private let size = NSSize(width: 240, height: 54)

    func prepare() {
        panel = NSPanel(contentRect: NSRect(origin: .zero, size: size),
                        styleMask: [.borderless, .nonactivatingPanel],
                        backing: .buffered,
                        defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .statusBar
        panel.ignoresMouseEvents = true
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]

        view = HUDView(frame: NSRect(origin: .zero, size: size))
        panel.contentView = view
    }

    private func place() {
        guard let screen = NSScreen.main else { return }
        let frame = screen.visibleFrame
        let origin = NSPoint(x: frame.midX - size.width / 2,
                             y: frame.minY + 130)
        panel.setFrameOrigin(origin)
    }

    func showListening() {
        dismiss?.cancel()
        place()
        view.mode = .listening
        view.levels = []
        panel.alphaValue = 1
        panel.orderFrontRegardless()
        startTicker()
    }

    func pushLevel(_ rms: Float) {
        guard view.mode == .listening else { return }
        // Compress RMS into something that looks lively but does not clip.
        lastLevelAt = Date()
        let shaped = min(1, pow(max(0, rms) * 14, 0.65))
        var levels = view.levels
        levels.append(shaped)
        if levels.count > 26 { levels.removeFirst(levels.count - 26) }
        view.levels = levels
    }

    func showTranscribing() {
        stopTicker()
        view.mode = .message
        view.message = "转写中…"
        panel.orderFrontRegardless()
    }

    func toast(_ text: String) {
        stopTicker()
        place()
        view.mode = .message
        view.message = text
        panel.alphaValue = 1
        panel.orderFrontRegardless()
        hideSoon(after: 1.5)
    }

    func hideSoon(after seconds: TimeInterval = 0.9) {
        dismiss?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.hide() }
        dismiss = work
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: work)
    }

    func hide() {
        stopTicker()
        panel.orderOut(nil)
    }

    private func startTicker() {
        stopTicker()
        ticker = Timer.scheduledTimer(withTimeInterval: 1.0 / 30.0, repeats: true) { [weak self] _ in
            guard let self, self.view.mode == .listening else { return }
            var levels = self.view.levels
            if levels.isEmpty {
                self.view.levels = [Float](repeating: 0, count: 26)
            } else if Date().timeIntervalSince(self.lastLevelAt) > 0.08 {
                // Quiet moment: let the bars settle instead of freezing.
                levels = levels.map { $0 * 0.8 }
                self.view.levels = levels
            }
            self.view.needsDisplay = true
        }
    }

    private func stopTicker() {
        ticker?.invalidate()
        ticker = nil
    }
}

final class HUDView: NSView {
    enum Mode { case listening, message }

    var mode: Mode = .listening { didSet { needsDisplay = true } }
    var message: String = "" { didSet { needsDisplay = true } }
    var levels: [Float] = [] { didSet { needsDisplay = true } }

    override var isFlipped: Bool { false }

    override func draw(_ dirtyRect: NSRect) {
        let pill = bounds.insetBy(dx: 1, dy: 1)
        let path = NSBezierPath(roundedRect: pill, xRadius: pill.height / 2, yRadius: pill.height / 2)
        NSColor(calibratedWhite: 0.06, alpha: 0.88).setFill()
        path.fill()
        NSColor(calibratedWhite: 1, alpha: 0.10).setStroke()
        path.lineWidth = 1
        path.stroke()

        switch mode {
        case .listening: drawBars()
        case .message: drawMessage()
        }
    }

    private func drawMessage() {
        let style = NSMutableParagraphStyle()
        style.alignment = .center
        style.lineBreakMode = .byTruncatingTail
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 13, weight: .medium),
            .foregroundColor: NSColor.white,
            .paragraphStyle: style,
        ]
        let rect = NSRect(x: 16, y: bounds.midY - 9, width: bounds.width - 32, height: 20)
        (message as NSString).draw(in: rect, withAttributes: attributes)
    }

    private func drawBars() {
        let iconSize: CGFloat = 20
        let iconX: CGFloat = 16
        NSColor.white.withAlphaComponent(0.85).set()

        if let image = NSImage(systemSymbolName: "mic.fill", accessibilityDescription: "mic") {
            let configured = image.withSymbolConfiguration(
                NSImage.SymbolConfiguration(pointSize: iconSize, weight: .semibold))
            let rect = NSRect(x: iconX, y: bounds.midY - 10, width: 16, height: 20)
            configured?.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 1.0)
        }

        let barWidth: CGFloat = 3
        let gap: CGFloat = 3
        let startX = iconX + 28
        let available = bounds.width - startX - 18
        let maxBars = max(1, Int((available + gap) / (barWidth + gap)))
        let shown = Array(levels.suffix(maxBars))
        let maxHeight = bounds.height - 20
        let totalWidth = CGFloat(shown.count) * (barWidth + gap) - gap
        var x = startX + (available - totalWidth) / 2
        let mid = bounds.midY

        for (index, level) in shown.enumerated() {
            let height = max(3, CGFloat(level) * maxHeight)
            let isLatest = index == shown.count - 1
            let color = isLatest
                ? NSColor(calibratedRed: 0.42, green: 0.78, blue: 1.0, alpha: 1)
                : NSColor.white.withAlphaComponent(0.55)
            color.setFill()
            let rect = NSRect(x: x, y: mid - height / 2, width: barWidth, height: height)
            NSBezierPath(roundedRect: rect, xRadius: barWidth / 2, yRadius: barWidth / 2).fill()
            x += barWidth + gap
        }
    }
}

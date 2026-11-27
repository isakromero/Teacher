import SwiftUI

/// Aristotle and his speech bubble, near the bottom of `place` (the main screen or the review's
/// summary). It never blocks anything outside itself; a tap or a swipe on it sends it away, and
/// it goes by itself after a few seconds (`MascotCenter`).
struct MascotOverlay: View {
    let center: MascotCenter
    let place: MascotSurface
    /// Space to keep free below it (the bottom bar and the page's Tutor button).
    var bottomInset: CGFloat = 0
    let perform: (MascotMessage.Action) -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(spacing: 0) {
            Spacer(minLength: 0)
            if let message = center.current, center.surface == place {
                MascotCard(message: message, start: center.shownAt ?? Date(), reduceMotion: reduceMotion,
                           dismiss: { center.dismiss(message.id) },
                           perform: { action in
                               center.dismiss(message.id)
                               perform(action)
                           })
                    .id(message.id)
                    .transition(reduceMotion ? .opacity
                                : .asymmetric(insertion: .move(edge: .bottom).combined(with: .opacity), removal: .opacity))
            }
        }
        .padding(.horizontal, 16)
        .padding(.bottom, bottomInset)
        .frame(maxWidth: .infinity, alignment: .leading)
        .animation(reduceMotion ? .easeInOut(duration: 0.25) : .spring(response: 0.42, dampingFraction: 0.78),
                   value: center.current?.id)
    }
}

/// One message: the figure, and the bubble beside it pointing at his head.
private struct MascotCard: View {
    let message: MascotMessage
    let start: Date
    let reduceMotion: Bool
    let dismiss: () -> Void
    let perform: (MascotMessage.Action) -> Void

    @State private var appeared = false
    @State private var drag: CGSize = .zero

    /// Points to a sprite pixel.
    static let pixel: CGFloat = 2

    var body: some View {
        HStack(alignment: .bottom, spacing: 0) {
            TimelineView(.animation(minimumInterval: 1.0 / 20, paused: reduceMotion)) { context in
                MascotTile(pose: MascotAnimation.pose(message.performance, elapsed: context.date.timeIntervalSince(start),
                                                      reduceMotion: reduceMotion),
                           pixel: Self.pixel)
            }
            bubble
                .padding(.bottom, 14)
        }
        .contentShape(.rect)
        .offset(x: drag.width * 0.4, y: max(0, drag.height))
        .onTapGesture(perform: dismiss)
        .gesture(
            DragGesture(minimumDistance: 10)
                .onChanged { drag = $0.translation }
                .onEnded { value in
                    if value.translation.height > 24 || abs(value.translation.width) > 60 {
                        dismiss()
                    } else {
                        withAnimation(.snappy) { drag = .zero }
                    }
                }
        )
        // One element for VoiceOver: the message, with its button and Dismiss as actions.
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Text("Aristotle: \(message.spoken)"))
        .accessibilityAddTraits(.isStaticText)
        .accessibilityAction(.default, dismiss)
        .accessibilityAction(.escape, dismiss)
        .accessibilityAction(named: Text("Dismiss"), dismiss)
        .modifier(ActionForAccessibility(action: message.action, perform: perform))
        .accessibilityIdentifier("mascot.message")
        .onAppear {
            appeared = true
            AccessibilityNotification.Announcement(message.spoken).post()
        }
        .sensoryFeedback(trigger: appeared) { _, shown in
            guard shown else { return nil }
            switch message.kind {
            case .pageFinished: return .success
            case .goalMet: return .impact(weight: .light) // as the plain "goal met" note
            case .welcomeBack: return nil
            }
        }
    }

    private var bubble: some View {
        VStack(alignment: .leading, spacing: 3) {
            ForEach(Array(message.lines.enumerated()), id: \.offset) { index, line in
                Text(line)
                    .font(index == 0 ? .subheadline.weight(.semibold) : .subheadline)
                    .foregroundStyle(index == 0 ? Color.pageForeground : Color.pageMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let action = message.action {
                Button(action.label) { perform(action) }
                    .font(.subheadline.weight(.semibold))
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
                    .tint(Color.pageAccent)
                    .foregroundStyle(Color.pageAccentForeground)
                    .padding(.top, 4)
                    .accessibilityIdentifier("mascot.action")
            }
        }
        .padding(.leading, 12 + BubbleShape.tail)
        .padding(.trailing, 14)
        .padding(.vertical, 10)
        .background {
            BubbleShape()
                .fill(Color.pageSurface)
                .shadow(color: .black.opacity(0.14), radius: 10, y: 3)
        }
        .overlay { BubbleShape().stroke(Color.pageBorderStrong, lineWidth: 1) }
        .frame(maxWidth: 290, alignment: .leading)
        .dynamicTypeSize(...DynamicTypeSize.accessibility3)
    }
}

/// The figure on a tile of the app icon's blue, so he reads over any page (as on the icon).
struct MascotTile: View {
    static let blue = Color(.sRGB, red: 0x28 / 255, green: 0x59 / 255, blue: 0x8A / 255, opacity: 1)

    let pose: MascotPose
    var pixel: CGFloat = 2

    var body: some View {
        MascotSpriteView(pose: pose, pixel: pixel)
            .padding(pixel * 2)
            .background(Self.blue, in: .rect(cornerRadius: pixel * 8, style: .continuous))
            .shadow(color: .black.opacity(0.18), radius: 6, y: 2)
    }
}

/// The bubble's button as a named VoiceOver action, when there is one.
private struct ActionForAccessibility: ViewModifier {
    let action: MascotMessage.Action?
    let perform: (MascotMessage.Action) -> Void

    func body(content: Content) -> some View {
        if let action {
            content.accessibilityAction(named: Text(action.label)) { perform(action) }
        } else {
            content
        }
    }
}

/// A rounded bubble with a small tail on its left edge, near the bottom, pointing at the figure.
struct BubbleShape: Shape {
    static let tail: CGFloat = 7
    var radius: CGFloat = 14

    func path(in rect: CGRect) -> Path {
        let left = rect.minX + Self.tail
        let corner = min(radius, max(4, (rect.height - 14) / 2), (rect.width - Self.tail) / 2)
        // The tail leaves the left edge just above the bottom corner and points down-left.
        let low = rect.maxY - corner - 2
        var path = Path()
        path.move(to: CGPoint(x: left + corner, y: rect.minY))
        path.addArc(tangent1End: CGPoint(x: rect.maxX, y: rect.minY), tangent2End: CGPoint(x: rect.maxX, y: rect.maxY), radius: corner)
        path.addArc(tangent1End: CGPoint(x: rect.maxX, y: rect.maxY), tangent2End: CGPoint(x: left, y: rect.maxY), radius: corner)
        path.addArc(tangent1End: CGPoint(x: left, y: rect.maxY), tangent2End: CGPoint(x: left, y: rect.minY), radius: corner)
        path.addLine(to: CGPoint(x: left, y: low))
        path.addLine(to: CGPoint(x: rect.minX, y: low + 4))
        path.addLine(to: CGPoint(x: left, y: low - 9))
        path.addArc(tangent1End: CGPoint(x: left, y: rect.minY), tangent2End: CGPoint(x: rect.maxX, y: rect.minY), radius: corner)
        path.closeSubpath()
        return path
    }
}

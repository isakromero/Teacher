import SwiftUI
import XCTest
@testable import Airistotle

/// Renders the mascot's frames and his messages to PNGs, for looking at. Runs only when
/// `MASCOT_SHOTS` names a folder (`TEST_RUNNER_MASCOT_SHOTS=… xcodebuild test`).
@MainActor
final class MascotRenderTests: XCTestCase {
    private func save(_ view: some View, _ name: String, folder: URL, scheme: ColorScheme, scale: CGFloat = 2) throws {
        let renderer = ImageRenderer(content: view.environment(\.colorScheme, scheme))
        renderer.scale = scale
        let image = try XCTUnwrap(renderer.uiImage, name)
        try XCTUnwrap(image.pngData()).write(to: folder.appending(path: "\(name).png"))
    }

    private func folder() throws -> URL {
        guard let path = ProcessInfo.processInfo.environment["MASCOT_SHOTS"], !path.isEmpty else {
            throw XCTSkip("MASCOT_SHOTS is not set")
        }
        let folder = URL(fileURLWithPath: path)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        return folder
    }

    /// Every frame, 10 points to a pixel, on the light and dark page backgrounds and the icon's
    /// blue; then at the size the app shows them.
    func testRenderSpriteSheet() throws {
        let folder = try folder()
        let grounds: [(String, Color)] = [("page, light", Color(PageColors.rgb(0xF7F5F0))), ("page, dark", Color(PageColors.rgb(0x141312))),
                                          ("icon blue", Color(PageColors.rgb(0x28598A)))]
        let sheet = VStack(alignment: .leading, spacing: 0) {
            ForEach(grounds.indices, id: \.self) { index in
                HStack(spacing: 12) {
                    ForEach(MascotPose.allCases, id: \.self) { pose in
                        VStack(spacing: 4) {
                            MascotSpriteView(pose: pose, pixel: 10)
                            Text(pose.rawValue).font(.caption.monospaced())
                                .foregroundStyle(index == 0 ? Color.black : Color.white)
                        }
                    }
                }
                .padding(16)
                .background(grounds[index].1)
            }
            HStack(alignment: .bottom, spacing: 24) {
                ForEach(grounds.indices, id: \.self) { index in
                    HStack(spacing: 4) {
                        ForEach(MascotPose.allCases, id: \.self) { MascotSpriteView(pose: $0, pixel: 2) }
                    }
                    .padding(10)
                    .background(grounds[index].1)
                }
            }
            .padding(16)
            .background(Color.gray.opacity(0.3))
        }
        try save(sheet, "mascot-sprite-sheet", folder: folder, scheme: .light, scale: 1)
    }

    /// The messages as the overlay draws them, light and dark, at the default and a large text size.
    func testRenderMessages() throws {
        let folder = try folder()
        let messages = [
            MascotText.pageFinished(slug: "waves", title: "Waves and superposition"),
            MascotText.goalMet(answers: 11),
            MascotText.welcomeBack(due: 7, atLeast: false, resumeTitle: nil),
            MascotText.welcomeBack(due: 0, atLeast: false, resumeTitle: "Waves and superposition")
        ]
        let centers = messages.map { message in
            let center = MascotCenter(store: MascotStore(defaults: UserDefaults(suiteName: "render-\(UUID().uuidString)") ?? .standard))
            center.setAvailable(.browser, true)
            center.raise(message)
            return center
        }
        for scheme in [ColorScheme.light, .dark] {
            for size in [DynamicTypeSize.large, .accessibility2] {
                let cards = VStack(spacing: 0) {
                    ForEach(centers.indices, id: \.self) { index in
                        MascotOverlay(center: centers[index], place: .browser) { _ in }
                            .frame(width: 402, height: size.isAccessibilitySize ? 330 : 170)
                            .background(Color.pageBackground)
                    }
                }
                .environment(\.dynamicTypeSize, size)
                let name = "mascot-messages-\(scheme == .light ? "light" : "dark")\(size.isAccessibilitySize ? "-large-type" : "")"
                try save(cards, name, folder: folder, scheme: scheme)
            }
        }
    }
}

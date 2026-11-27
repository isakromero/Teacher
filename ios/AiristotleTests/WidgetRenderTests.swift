import SwiftUI
import WidgetKit
import XCTest
@testable import Airistotle

/// Renders every widget family in its main states to PNG contact sheets, for looking at. Runs
/// only when `WIDGET_SHOTS` names a folder (`TEST_RUNNER_WIDGET_SHOTS=… xcodebuild test`).
/// Rendered outside WidgetKit: the home-screen sizes get the widget's own background on a
/// rounded tile; the lock-screen families are drawn in white on a dark backdrop (the system
/// would draw them in its vibrant style).
@MainActor
final class WidgetRenderTests: XCTestCase {
    private static let zone = TimeZone.current

    private func moment(_ state: ReviewState, goal: GoalStatus?, stale: Bool = false) -> ReviewMoment {
        let now = Date()
        return ReviewMoment(date: now, state: state, goal: goal, stale: stale, asOf: now.addingTimeInterval(stale ? -5 * 3600 : 0),
                            source: .server, zone: Self.zone)
    }

    private var states: [(String, WidgetDisplay)] {
        let today = LocalDay(Date(), in: Self.zone)
        let going = GoalStatus(today: today, answersToday: 6, goal: 10, streak: 4, best: 9, windowDays: 60, asOf: Date())
        let met = GoalStatus(today: today, answersToday: 12, goal: 10, streak: 5, best: 9, windowDays: 60, asOf: Date())
        let none = GoalStatus(today: today, answersToday: 0, goal: 10, streak: 0, best: 2, windowDays: 60, asOf: Date())
        let later = Date().addingTimeInterval(3 * 3600)
        let tomorrow = LocalDay(Date(), in: Self.zone).adding(days: 1).start(in: Self.zone).addingTimeInterval(9 * 3600)
        return [
            ("7 due", .reviews(moment(.due(7, atLeast: false), goal: going), continueTitle: "Waves and superposition")),
            ("All done today", .reviews(moment(.doneToday(next: tomorrow), goal: met), continueTitle: "Refraction")),
            ("Nothing due until", .reviews(moment(.nothingDue(next: later), goal: none), continueTitle: nil)),
            ("Stale (5 h old)", .reviews(moment(.due(3, atLeast: false), goal: going, stale: true), continueTitle: "Refraction")),
            ("Signed out", .signedOut),
            ("No data yet", .noData),
            ("App Group missing", .unavailable(SharedContainer.Failure.unavailable(
                "the App Group group.dev.airistotle.reader is not available to this build.").localizedDescription))
        ]
    }

    private func tile(_ display: WidgetDisplay, _ family: WidgetFamily, size: CGSize) -> some View {
        ReviewWidgetView(display: display, family: family)
            .padding(16)
            .frame(width: size.width, height: size.height)
            .background(Color.pageBackground, in: .rect(cornerRadius: 22))
            .overlay(RoundedRectangle(cornerRadius: 22).stroke(Color.pageBorder))
    }

    private func lock(_ display: WidgetDisplay, _ family: WidgetFamily, size: CGSize) -> some View {
        ReviewWidgetView(display: display, family: family)
            .frame(width: size.width, height: size.height)
            .foregroundStyle(.white)
            .environment(\.colorScheme, .dark)
    }

    private func save(_ view: some View, _ name: String, folder: URL, scheme: ColorScheme) throws {
        let renderer = ImageRenderer(content: view.environment(\.colorScheme, scheme))
        renderer.scale = 2
        let image = try XCTUnwrap(renderer.uiImage, name)
        try XCTUnwrap(image.pngData()).write(to: folder.appending(path: "\(name).png"))
    }

    func testRenderWidgetStates() throws {
        guard let path = ProcessInfo.processInfo.environment["WIDGET_SHOTS"], !path.isEmpty else {
            throw XCTSkip("WIDGET_SHOTS is not set")
        }
        let folder = URL(fileURLWithPath: path)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        for scheme in [ColorScheme.light, .dark] {
            let sheet = VStack(alignment: .leading, spacing: 18) {
                ForEach(self.states.indices, id: \.self) { index in
                    let (label, display) = self.states[index]
                    VStack(alignment: .leading, spacing: 6) {
                        Text(label).font(.headline).foregroundStyle(Color.pageForeground)
                        HStack(alignment: .top, spacing: 18) {
                            self.tile(display, .systemSmall, size: CGSize(width: 170, height: 170))
                            self.tile(display, .systemMedium, size: CGSize(width: 364, height: 170))
                        }
                    }
                }
            }
            .padding(24)
            .background(Color.pageSurface2)
            try save(sheet, "widgets-home-\(scheme == .light ? "light" : "dark")", folder: folder, scheme: scheme)
        }
        let lockSheet = VStack(alignment: .leading, spacing: 16) {
            ForEach(self.states.indices, id: \.self) { index in
                let (label, display) = self.states[index]
                VStack(alignment: .leading, spacing: 6) {
                    Text(label).font(.headline).foregroundStyle(.white)
                    HStack(alignment: .center, spacing: 20) {
                        self.lock(display, .accessoryCircular, size: CGSize(width: 72, height: 72))
                        self.lock(display, .accessoryRectangular, size: CGSize(width: 172, height: 76))
                        self.lock(display, .accessoryInline, size: CGSize(width: 260, height: 24))
                    }
                }
            }
        }
        .padding(24)
        .background(LinearGradient(colors: [Color(white: 0.18), Color(white: 0.32)], startPoint: .top, endPoint: .bottom))
        try save(lockSheet, "widgets-lock-screen", folder: folder, scheme: .dark)
    }
}

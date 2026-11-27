import SwiftUI
import WidgetKit

/// The widget extension: reviews due, on the home screen and the lock screen. It reads only the
/// snapshot the app leaves in the App Group (`SharedContainer`); it has no session and makes no
/// requests of its own.
@main
struct AiristotleWidgets: WidgetBundle {
    var body: some Widget {
        ReviewWidget()
    }
}

struct ReviewEntry: TimelineEntry {
    let date: Date
    let display: WidgetDisplay
}

struct ReviewTimelineProvider: TimelineProvider {
    func placeholder(in context: Context) -> ReviewEntry {
        ReviewEntry(date: Date(), display: Self.sample)
    }

    func getSnapshot(in context: Context, completion: @escaping (ReviewEntry) -> Void) {
        if context.isPreview {
            completion(ReviewEntry(date: Date(), display: Self.sample))
            return
        }
        let now = Date()
        completion(ReviewEntry(date: now, display: ReviewProjection.display(Self.read(), at: now)))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<ReviewEntry>) -> Void) {
        let now = Date()
        let (entries, reloadAt) = ReviewProjection.timeline(Self.read(), now: now)
        let policy: TimelineReloadPolicy = reloadAt.map { .after($0) } ?? .never
        completion(Timeline(entries: entries.map { ReviewEntry(date: $0.0, display: $0.1) }, policy: policy))
    }

    static func read() -> Result<WidgetSnapshot?, SharedContainer.Failure> {
        SharedContainer.live().flatMap(\.snapshotRead)
    }

    /// The gallery's example: a few reviews due and a goal under way.
    static var sample: WidgetDisplay {
        let now = Date()
        let zone = TimeZone.current
        let today = LocalDay(now, in: zone)
        let moment = ReviewMoment(
            date: now, state: .due(6, atLeast: false),
            goal: GoalStatus(today: today, answersToday: 4, goal: 10, streak: 3, best: 5, windowDays: 60, asOf: now),
            stale: false, asOf: now, source: .server, zone: zone)
        return .reviews(moment, continueTitle: "Waves and superposition")
    }
}

struct ReviewWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "dev.airistotle.reader.reviews", provider: ReviewTimelineProvider()) { entry in
            ReviewWidgetEntryView(entry: entry)
        }
        .configurationDisplayName("Reviews due")
        .description("How many review questions are due, today's goal and your streak. Tap to start the review.")
        .supportedFamilies([.systemSmall, .systemMedium, .accessoryCircular, .accessoryRectangular, .accessoryInline])
    }
}

struct ReviewWidgetEntryView: View {
    let entry: ReviewEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        ReviewWidgetView(display: entry.display, family: family)
            .widgetURL(ReviewWidgetView.link(entry.display))
            .containerBackground(for: .widget) {
                if family == .systemSmall || family == .systemMedium { Color.pageBackground } else { Color.clear }
            }
    }
}

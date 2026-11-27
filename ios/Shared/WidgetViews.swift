import SwiftUI
import WidgetKit

/// The review-due widget's views, one per family. They live with the shared code so the app's
/// tests can render them too (the screenshots of every state).
struct ReviewWidgetView: View {
    let display: WidgetDisplay
    let family: WidgetFamily

    var body: some View {
        switch family {
        case .accessoryCircular: ReviewCircular(display: display)
        case .accessoryRectangular: ReviewRectangular(display: display)
        case .accessoryInline: ReviewInline(display: display)
        case .systemMedium: ReviewMedium(display: display)
        default: ReviewSmall(display: display)
        }
    }

    /// Where a tap on the widget goes: the review, when there is something to review.
    static func link(_ display: WidgetDisplay) -> URL? {
        if case .reviews = display { DeepLink.review(quick: false).url } else { nil }
    }
}

// MARK: - Home screen

/// The parts every home-screen size shows.
private struct ReviewHeadline: View {
    let moment: ReviewMoment
    var compact = false

    var body: some View {
        let (title, detail) = WidgetText.headline(moment)
        VStack(alignment: .leading, spacing: 2) {
            switch moment.state {
            case .due(let count, let atLeast):
                Text("\(count)\(atLeast ? "+" : "")")
                    .font(.system(size: compact ? 40 : 46, weight: .semibold, design: .rounded).monospacedDigit())
                    .foregroundStyle(Color.pageAccent)
                    .widgetAccentable()
                    .minimumScaleFactor(0.6)
                    .lineLimit(1)
                Text(count == 1 && !atLeast ? "review due" : "reviews due")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Color.pageForeground)
            case .doneToday:
                Image(systemName: "checkmark.circle.fill")
                    .font(.title2)
                    .foregroundStyle(Color.pageOK)
                    .widgetAccentable()
                Text(title).font(.headline).foregroundStyle(Color.pageForeground)
                if let detail { Text(detail).font(.caption).foregroundStyle(Color.pageMuted) }
            case .nothingDue:
                Image(systemName: "clock")
                    .font(.title2)
                    .foregroundStyle(Color.pageMuted)
                Text(title).font(.headline).foregroundStyle(Color.pageForeground)
                if let detail { Text(detail).font(.caption).foregroundStyle(Color.pageMuted) }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel([title, detail].compactMap(\.self).joined(separator: ", "))
    }
}

private struct GoalLine: View {
    let goal: GoalStatus

    var body: some View {
        HStack(spacing: 6) {
            GoalRingGlyph(fraction: goal.fraction, met: goal.met)
                .frame(width: 14, height: 14)
            Text(WidgetText.goal(goal))
                .font(.caption.monospacedDigit())
                .foregroundStyle(Color.pageForeground)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
        }
        .accessibilityElement(children: .combine)
    }
}

/// A small ring for today's goal (the app's `GoalRing` in miniature).
struct GoalRingGlyph: View {
    let fraction: Double
    let met: Bool
    var lineWidth: CGFloat = 3

    var body: some View {
        ZStack {
            Circle().stroke(Color.pageBorderStrong, lineWidth: lineWidth)
            Circle()
                .trim(from: 0, to: max(fraction, 0.001))
                .stroke(met ? Color.pageOK : Color.pageAccent, style: StrokeStyle(lineWidth: lineWidth, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .widgetAccentable()
        }
        .accessibilityHidden(true)
    }
}

private struct StaleLine: View {
    let moment: ReviewMoment

    var body: some View {
        if let text = WidgetText.asOf(moment) {
            Label(text, systemImage: "clock.arrow.circlepath")
                .font(.caption2)
                .foregroundStyle(Color.pageWarn)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
        }
    }
}

/// Signed out, no data, unreadable: said plainly.
private struct NoticeView: View {
    let display: WidgetDisplay

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Image(systemName: icon).font(.title3).foregroundStyle(Color.pageMuted)
            Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(Color.pageForeground)
                .fixedSize(horizontal: false, vertical: true)
            if let detail {
                Text(detail).font(.caption2).foregroundStyle(Color.pageMuted).lineLimit(4)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private var icon: String {
        switch display {
        case .unavailable: "exclamationmark.triangle"
        case .signedOut: "person.crop.circle"
        default: "arrow.down.circle"
        }
    }

    private var title: String {
        switch display {
        case .unavailable: WidgetText.unavailableTitle
        case .signedOut: WidgetText.signedOut
        default: WidgetText.noData
        }
    }

    private var detail: String? {
        if case .unavailable(let reason) = display { reason } else { nil }
    }
}

private struct WidgetTitle: View {
    var body: some View {
        Label("Review", systemImage: "rectangle.stack")
            .font(.caption.weight(.semibold))
            .foregroundStyle(Color.pageMuted)
            .labelStyle(.titleAndIcon)
    }
}

private struct ReviewSmall: View {
    let display: WidgetDisplay

    var body: some View {
        switch display {
        case .reviews(let moment, _):
            VStack(alignment: .leading, spacing: 4) {
                WidgetTitle()
                Spacer(minLength: 0)
                ReviewHeadline(moment: moment, compact: true)
                Spacer(minLength: 0)
                if let stale = WidgetText.asOf(moment) {
                    Label(stale, systemImage: "clock.arrow.circlepath").font(.caption2).foregroundStyle(Color.pageWarn).lineLimit(1)
                        .minimumScaleFactor(0.8)
                } else if let goal = moment.goal {
                    GoalLine(goal: goal)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        default:
            NoticeView(display: display)
        }
    }
}

private struct ReviewMedium: View {
    let display: WidgetDisplay

    var body: some View {
        switch display {
        case .reviews(let moment, let continueTitle):
            HStack(alignment: .top, spacing: 14) {
                VStack(alignment: .leading, spacing: 4) {
                    WidgetTitle()
                    Spacer(minLength: 0)
                    ReviewHeadline(moment: moment)
                    Spacer(minLength: 0)
                    StaleLine(moment: moment)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
                VStack(alignment: .leading, spacing: 8) {
                    if let goal = moment.goal {
                        HStack(spacing: 8) {
                            GoalRingGlyph(fraction: goal.fraction, met: goal.met, lineWidth: 4).frame(width: 30, height: 30)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(WidgetText.goal(goal)).font(.caption.weight(.semibold).monospacedDigit())
                                    .foregroundStyle(Color.pageForeground)
                                Text(WidgetText.streak(goal) ?? "No streak yet").font(.caption2).foregroundStyle(Color.pageMuted)
                            }
                        }
                        .accessibilityElement(children: .combine)
                    }
                    Spacer(minLength: 0)
                    if let continueTitle {
                        Link(destination: DeepLink.continueStudying.url) {
                            VStack(alignment: .leading, spacing: 1) {
                                Label("Continue studying", systemImage: "book")
                                    .font(.caption.weight(.semibold))
                                    .foregroundStyle(Color.pageAccent)
                                Text(continueTitle).font(.caption2).foregroundStyle(Color.pageForeground).lineLimit(2)
                            }
                            .padding(8)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(Color.pageAccentSoft, in: .rect(cornerRadius: 10))
                        }
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            }
        default:
            NoticeView(display: display)
        }
    }
}

// MARK: - Lock screen

private struct ReviewCircular: View {
    let display: WidgetDisplay

    var body: some View {
        ZStack {
            AccessoryWidgetBackground()
            switch display {
            case .reviews(let moment, _):
                switch moment.state {
                case .due(let count, let atLeast):
                    VStack(spacing: -2) {
                        Text("\(count)\(atLeast ? "+" : "")").font(.system(size: 22, weight: .semibold, design: .rounded))
                            .minimumScaleFactor(0.5)
                        Text("due").font(.caption2)
                    }
                case .doneToday:
                    VStack(spacing: 0) {
                        Image(systemName: "checkmark").font(.system(size: 18, weight: .semibold))
                        Text("done").font(.caption2)
                    }
                case .nothingDue:
                    VStack(spacing: -2) {
                        Text("0").font(.system(size: 22, weight: .semibold, design: .rounded))
                        Text("due").font(.caption2)
                    }
                }
            case .signedOut:
                Image(systemName: "person.crop.circle.badge.exclamationmark").font(.title3)
            case .noData:
                Image(systemName: "arrow.down.circle").font(.title3)
            case .unavailable:
                Image(systemName: "exclamationmark.triangle").font(.title3)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(ReviewInline.text(display))
    }
}

private struct ReviewRectangular: View {
    let display: WidgetDisplay

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            switch display {
            case .reviews(let moment, _):
                let (title, detail) = WidgetText.headline(moment)
                Text(title).font(.headline).widgetAccentable().lineLimit(1)
                if let detail { Text(detail).font(.caption).lineLimit(1) }
                if let stale = WidgetText.asOf(moment) {
                    Text(stale).font(.caption2).lineLimit(1)
                } else if let goal = moment.goal {
                    Text([WidgetText.goal(goal), WidgetText.streak(goal)].compactMap(\.self).joined(separator: " · "))
                        .font(.caption).lineLimit(1).minimumScaleFactor(0.8)
                }
            case .signedOut:
                Text("Airistotle").font(.headline)
                Text(WidgetText.signedOut).font(.caption).lineLimit(2)
            case .noData:
                Text("Airistotle").font(.headline)
                Text(WidgetText.noData).font(.caption).lineLimit(2)
            case .unavailable:
                Text("Airistotle").font(.headline)
                Text(WidgetText.unavailableTitle).font(.caption).lineLimit(2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct ReviewInline: View {
    let display: WidgetDisplay

    var body: some View {
        Text(Self.text(display))
    }

    static func text(_ display: WidgetDisplay) -> String {
        switch display {
        case .reviews(let moment, _):
            let (title, detail) = WidgetText.headline(moment)
            switch moment.state {
            case .due(let count, let atLeast):
                guard moment.stale else { return title }
                // Short enough for the line above the clock.
                return "\(count)\(atLeast ? "+" : "") due, as of \(WidgetText.updated(moment.asOf, from: moment.date, zone: moment.zone))"
            case .doneToday: return "Reviews: all done today"
            case .nothingDue: return [title, detail].compactMap(\.self).joined(separator: " ")
            }
        case .signedOut: return "Airistotle: sign in"
        case .noData: return "Airistotle: open to load reviews"
        case .unavailable: return "Airistotle: data unavailable"
        }
    }
}

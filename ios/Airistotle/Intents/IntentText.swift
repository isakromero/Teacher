import Foundation

/// What the "Reviews due" shortcut answers, from the widget's snapshot (pure, tested).
enum IntentText {
    struct Answer: Equatable {
        var count: Int
        var dialog: String
    }

    struct Failure: Error, Equatable, LocalizedError {
        var message: String
        var errorDescription: String? { message }
    }

    static func reviewsDue(_ display: WidgetDisplay) -> Result<Answer, Failure> {
        switch display {
        case .unavailable(let reason):
            return .failure(Failure(message: "Airistotle cannot read its review data: \(reason)"))
        case .signedOut:
            return .failure(Failure(message: "You are not signed in to Airistotle. Open the app and sign in first."))
        case .noData:
            return .failure(Failure(message: "Airistotle has not loaded your reviews yet. Open the app once while it can reach "
                + "your server."))
        case .reviews(let moment, _):
            let count = moment.state.dueCount
            let stale = moment.stale
                ? " That is as of \(WidgetText.updated(moment.asOf, from: moment.date, zone: moment.zone)), when Airistotle last updated."
                : ""
            let text: String
            switch moment.state {
            case .due(let due, let atLeast):
                let many = atLeast ? "At least \(due) reviews are" : (due == 1 ? "1 review is" : "\(due) reviews are")
                text = "\(many) due in Airistotle."
            case .doneToday(let next):
                let tomorrow = LocalDay(moment.date, in: moment.zone).adding(days: 1).start(in: moment.zone)
                let after = switch next {
                case nil: "nothing else is scheduled"
                case tomorrow?: "more are due tomorrow"
                case let next?: "the next is due \(WidgetText.when(next, from: moment.date, zone: moment.zone))"
                }
                text = "No reviews are due. You are done for today: \(after)."
            case .nothingDue(let next):
                text = next.map { "No reviews are due. The next is due \(WidgetText.when($0, from: moment.date, zone: moment.zone))." }
                    ?? "No reviews are due, and none is scheduled."
            }
            return .success(Answer(count: count, dialog: text + stale))
        }
    }
}

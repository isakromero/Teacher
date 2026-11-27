import AppIntents
import Foundation

// The app's actions for Shortcuts, Spotlight and Siri. Those that show something open the app
// and go through the same links as the widget (`DeepLink`); "Reviews due" answers from the
// widget's snapshot without opening the app.

struct StartReviewIntent: AppIntent {
    static let title: LocalizedStringResource = "Start Review"
    static let description = IntentDescription("Opens the review of the questions due in Airistotle.")
    static let supportedModes: IntentModes = .foreground

    @Dependency private var model: AppModel

    @MainActor
    func perform() async throws -> some IntentResult {
        model.open(.review(quick: false))
        return .result()
    }
}

struct QuickReviewIntent: AppIntent {
    static let title: LocalizedStringResource = "Quick Review"
    static let description = IntentDescription("Opens a timed review in Airistotle: questions until the time you set in Settings is up.")
    static let supportedModes: IntentModes = .foreground

    @Dependency private var model: AppModel

    @MainActor
    func perform() async throws -> some IntentResult {
        model.open(.review(quick: true))
        return .result()
    }
}

struct ContinueStudyingIntent: AppIntent {
    static let title: LocalizedStringResource = "Continue Studying"
    static let description = IntentDescription("Opens the study page you were last working on in Airistotle.")
    static let supportedModes: IntentModes = [.background, .foreground(.dynamic)]

    @Dependency private var model: AppModel

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let snapshot: WidgetSnapshot?
        do {
            snapshot = try SharedContainer.live().get().readSnapshot()
        } catch {
            throw IntentText.Failure(message: "Airistotle cannot read its data: \(error.localizedDescription)")
        }
        guard let snapshot, snapshot.signedIn else {
            throw IntentText.Failure(message: "You are not signed in to Airistotle. Open the app and sign in first.")
        }
        guard let title = snapshot.continueTitle else {
            return .result(dialog: "There is no page to continue yet. Open a study page in Airistotle first.")
        }
        try await continueInForeground(nil, alwaysConfirm: false)
        model.open(.continueStudying)
        return .result(dialog: "Opening \(title).")
    }
}

struct ReviewsDueIntent: AppIntent {
    static let title: LocalizedStringResource = "Reviews Due"
    static let description = IntentDescription("How many review questions are due in Airistotle, without opening the app.")
    static let supportedModes: IntentModes = .background

    func perform() async throws -> some IntentResult & ReturnsValue<Int> & ProvidesDialog {
        let display = ReviewProjection.display(SharedContainer.readLive(), at: Date())
        switch IntentText.reviewsDue(display) {
        case .success(let answer):
            return .result(value: answer.count, dialog: "\(answer.dialog)")
        case .failure(let failure):
            throw failure
        }
    }
}

struct AddToAiristotleIntent: AppIntent {
    static let title: LocalizedStringResource = "Add to Airistotle"
    static let description = IntentDescription(
        "Opens Airistotle's Add page with a web address filled in. Nothing is fetched or added until you press Fetch and Add.")
    static let supportedModes: IntentModes = .foreground

    @Parameter(title: "Web address", description: "The address of the work to add (http or https).")
    var address: URL

    @Dependency private var model: AppModel

    static var parameterSummary: some ParameterSummary {
        Summary("Add \(\.$address) to Airistotle")
    }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard DeepLink.isAddableAddress(address) else {
            throw IntentText.Failure(message: "Airistotle can add only web addresses that start with http:// or https:// "
                + "(not \(address.absoluteString)).")
        }
        model.open(.add(address))
        return .result(dialog: "The Add page opens with the address filled in. Press Fetch, then Add, to add it.")
    }
}

extension IntentText.Failure: CustomLocalizedStringResourceConvertible {
    var localizedStringResource: LocalizedStringResource { "\(message)" }
}

struct AiristotleShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: StartReviewIntent(), phrases: [
            "Start review in \(.applicationName)",
            "Review with \(.applicationName)",
            "Start my \(.applicationName) review"
        ], shortTitle: "Start Review", systemImageName: "rectangle.stack")
        AppShortcut(intent: QuickReviewIntent(), phrases: [
            "Quick review in \(.applicationName)",
            "Start a quick \(.applicationName) review"
        ], shortTitle: "Quick Review", systemImageName: "timer")
        AppShortcut(intent: ContinueStudyingIntent(), phrases: [
            "Continue studying in \(.applicationName)",
            "Resume \(.applicationName)"
        ], shortTitle: "Continue Studying", systemImageName: "book")
        AppShortcut(intent: ReviewsDueIntent(), phrases: [
            "How many reviews are due in \(.applicationName)",
            "Reviews due in \(.applicationName)",
            "\(.applicationName) reviews due"
        ], shortTitle: "Reviews Due", systemImageName: "number")
        AppShortcut(intent: AddToAiristotleIntent(), phrases: [
            "Add to \(.applicationName)",
            "Add a link to \(.applicationName)"
        ], shortTitle: "Add to Airistotle", systemImageName: "plus")
    }
}

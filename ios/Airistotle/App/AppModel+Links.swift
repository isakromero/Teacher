import Foundation

/// What the browser does for a link: open a review over the page, or load one of the server's
/// own pages.
struct RoutedLink: Identifiable, Equatable {
    enum Action: Equatable {
        case review(ReviewRequest.Kind)
        case load(String)
    }

    let id = UUID()
    var action: Action
}

/// The app's links (`DeepLink`): from the widget, a reminder, a Shortcut or Spotlight.
extension AppModel {
    /// An `airistotle://` link was opened; a link the app does not know is refused, and says why.
    func open(url: URL) {
        do {
            open(try DeepLink.parse(url))
        } catch {
            linkProblem = error.message
        }
    }

    func open(_ link: DeepLink) {
        switch phase {
        case .launching:
            pendingLink = link // carried out once the saved session is restored
        case .signedOut:
            pendingLink = link
            notePendingLinkWhileSignedOut()
        case .signedIn:
            switch Self.action(for: link, resume: habits.currentResume(), quickMinutes: offline.quickReviewMinutes) {
            case .success(let action): routedLink = RoutedLink(action: action)
            case .failure(let refused): linkProblem = refused.message
            }
        }
    }

    /// Signed out with a link waiting: say that it opens after signing in.
    func notePendingLinkWhileSignedOut() {
        guard let pendingLink else { return }
        linkProblem = "Sign in first: \(Self.describe(pendingLink)) opens once you are signed in."
    }

    /// Where a link leads, for the account signed in. Only the server's own pages are loaded.
    nonisolated static func action(for link: DeepLink, resume: PhoneSummary.Resume?, quickMinutes: Int)
        -> Result<RoutedLink.Action, DeepLink.Refused> {
        switch link {
        case .review(let quick):
            return .success(.review(quick ? .quick(minutes: quickMinutes) : .all))
        case .continueStudying:
            guard let resume else {
                return .failure(.init(message: "There is no page to continue yet. Open a study page first; once you have studied "
                    + "one, Continue studying opens it."))
            }
            guard let target = PhoneSummary.resumeTarget(resume.target) else {
                return .failure(.init(message: "The server named a page to continue that the app does not open (\(resume.target))."))
            }
            return .success(.load(target))
        case .page(let slug):
            return .success(.load("/artifacts/\(StudyPageRequests.segment(slug))"))
        case .add(let address):
            // The Add page fills in the address (`/add?url=`); nothing is fetched or added until
            // the reader presses Fetch and then Add.
            return .success(.load("/add?" + StudyPageRequests.formEncode([("url", address.absoluteString)])))
        }
    }

    nonisolated static func describe(_ link: DeepLink) -> String {
        switch link {
        case .review(let quick): quick ? "the quick review" : "the review"
        case .continueStudying: "the page to continue"
        case .page: "the study page"
        case .add: "the Add page"
        }
    }

    /// A saved page chosen in Spotlight: open it, if it belongs to the account signed in.
    func openSpotlightItem(_ identifier: String) {
        guard let (namespace, slug) = SpotlightEntry.parse(identifier), DeepLink.isValidSlug(slug) else {
            linkProblem = "Airistotle cannot open this search result (\(identifier))."
            return
        }
        if case .signedIn = phase, gateway.connection?.cacheNamespace != namespace {
            linkProblem = "This page was saved under another server or account than the one signed in now, so it is not opened."
            return
        }
        open(.page(slug: slug))
    }
}

import Foundation

/// Tells when a study page was finished by an answer made in the app, from what the page's
/// replies said (`ReadingReport`), at most once per page for a server and account.
///
/// Only a change seen from not complete to complete counts: the page's blocks were loaded
/// while it was not complete (`state(complete: false)`), then an answer's reply said it is
/// (`answered(complete: true)`). A page that was already complete when it was opened, an
/// answer whose page was never seen incomplete (a revealed gate answered again on a finished
/// page, say), or a page congratulated before (`load`) gives nothing.
///
/// The pages already congratulated are kept by the caller (`MascotStore`) and handed in per
/// namespace with `load`; until a namespace is loaded, nothing in it is congratulated (when
/// they cannot be read, the reader is not congratulated twice for a page).
struct PageCompletionWatcher {
    struct Key: Hashable {
        var namespace: String
        var slug: String
    }

    struct Completion: Equatable {
        var namespace: String
        var slug: String
        /// The page's title, when its reply was seen.
        var title: String?
    }

    /// The last completeness seen for each page.
    private(set) var complete: [Key: Bool] = [:]
    private(set) var titles: [Key: String] = [:]
    /// The pages congratulated, per namespace (only loaded namespaces).
    private(set) var congratulated: [String: Set<String>] = [:]

    func isLoaded(_ namespace: String) -> Bool { congratulated[namespace] != nil }

    /// The pages congratulated before in `namespace` (from the store).
    mutating func load(_ namespace: String, slugs: Set<String>) {
        congratulated[namespace, default: []].formUnion(slugs)
    }

    /// Take in one report; a page to congratulate, if this report finished it.
    mutating func observe(_ report: ReadingReport) -> Completion? {
        let key = Key(namespace: report.namespace, slug: report.slug)
        switch report.kind {
        case .title(let title):
            titles[key] = title
            return nil
        case .state(let isComplete):
            complete[key] = isComplete
            return nil
        case .answered(let isComplete):
            let before = complete[key]
            complete[key] = isComplete
            guard before == false, isComplete, var done = congratulated[report.namespace], !done.contains(report.slug) else {
                return nil
            }
            done.insert(report.slug)
            congratulated[report.namespace] = done
            return Completion(namespace: report.namespace, slug: report.slug, title: titles[key])
        }
    }
}

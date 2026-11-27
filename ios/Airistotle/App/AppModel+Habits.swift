import Foundation

/// The phone summary's inputs from the rest of the app (see `HabitCenter`).
extension AppModel {
    /// The answers made on this phone and the saved review questions, for the daily goal.
    nonisolated static func localHabitData(_ namespace: String, pages: SavedStore, review: ReviewStore) async throws -> LocalHabitData {
        let attempts = await pages.pages(namespace).flatMap(\.pending).map(\.madeAt)
        return LocalHabitData(queue: try await review.queue(namespace), pageAttempts: attempts,
                              reviewsWaiting: try await review.pending(namespace).map(\.madeAt),
                              reviewsAnswered: try await review.answered(namespace))
    }

    /// The saved pages or the answers waiting changed: the snapshot and Spotlight follow.
    func offlineChanged() {
        habits.refreshSoon(fetch: false, delay: .milliseconds(300))
        guard case .signedIn = phase, let namespace = gateway.connection?.cacheNamespace else { return }
        let pages = offline.pages
        Task { await habits.spotlight.update(namespace: namespace, pages: pages) }
    }
}

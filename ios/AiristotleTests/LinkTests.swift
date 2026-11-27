import XCTest
@testable import Airistotle

final class DeepLinkTests: XCTestCase {
    private func parse(_ text: String) throws -> DeepLink {
        try DeepLink.parse(try XCTUnwrap(URL(string: text)))
    }

    private func refused(_ text: String, file: StaticString = #filePath, line: UInt = #line) {
        guard let url = URL(string: text) else { return } // not even a URL: nothing can open it
        XCTAssertThrowsError(try DeepLink.parse(url), text, file: file, line: line) { error in
            XCTAssertTrue((error as? DeepLink.Refused)?.message.hasPrefix("Airistotle cannot open the link") == true,
                          file: file, line: line)
        }
    }

    func testTheAppsLinks() throws {
        XCTAssertEqual(try parse("airistotle://review"), .review(quick: false))
        XCTAssertEqual(try parse("airistotle://review/"), .review(quick: false))
        XCTAssertEqual(try parse("AIRISTOTLE://Review/quick"), .review(quick: true))
        XCTAssertEqual(try parse("airistotle://continue"), .continueStudying)
        XCTAssertEqual(try parse("airistotle://page/waves-2"), .page(slug: "waves-2"))
        XCTAssertEqual(try parse("airistotle://add?url=https%3A%2F%2Farxiv.org%2Fabs%2F1706.03762"),
                       .add(try XCTUnwrap(URL(string: "https://arxiv.org/abs/1706.03762"))))
    }

    func testEveryLinkMadeIsOneTheRouterReads() throws {
        let links: [DeepLink] = [.review(quick: false), .review(quick: true), .continueStudying, .page(slug: "optics_1.b"),
                                 .add(try XCTUnwrap(URL(string: "https://example.com/paper?id=3&x=a%20b")))]
        for link in links {
            XCTAssertEqual(try DeepLink.parse(link.url), link, link.url.absoluteString)
        }
        XCTAssertEqual(DeepLink.review(quick: false).url.absoluteString, "airistotle://review")
        XCTAssertEqual(DeepLink.review(quick: true).url.absoluteString, "airistotle://review/quick")
    }

    func testUnknownOrMalformedLinksAreRefusedWithAMessage() {
        refused("https://airistotle.example/review")
        refused("airistotle://")
        refused("airistotle://settings")
        refused("airistotle://review/everything")
        refused("airistotle://review?concept=x")
        refused("airistotle://review#top")
        refused("airistotle://continue/waves")
        refused("airistotle://page")
        refused("airistotle://page/")
        refused("airistotle://page/a/b")
        refused("airistotle://page/..")
        refused("airistotle://page/%2E%2E%2Fadmin")
        refused("airistotle://page/x?y=1")
        refused("airistotle://add")
        refused("airistotle://add?url=javascript:alert(1)")
        refused("airistotle://add?url=file:///etc/passwd")
        refused("airistotle://add?url=teacher://app/admin")
        refused("airistotle://add?url=https://a.example&url=https://b.example")
        refused("airistotle://add?url=https://user:pass@a.example/")
        refused("airistotle://add/more?url=https://a.example")
        refused("airistotle://user@review")
    }

    func testWhereEachLinkLeadsIsAlwaysTheServersOwnPage() throws {
        let resume = PhoneSummary.Resume(slug: "waves", title: "Waves", target: "/artifacts/waves")
        XCTAssertEqual(try AppModel.action(for: .review(quick: true), resume: nil, quickMinutes: 5).get(), .review(.quick(minutes: 5)))
        XCTAssertEqual(try AppModel.action(for: .review(quick: false), resume: nil, quickMinutes: 5).get(), .review(.all))
        XCTAssertEqual(try AppModel.action(for: .continueStudying, resume: resume, quickMinutes: 5).get(), .load("/artifacts/waves"))
        XCTAssertEqual(try AppModel.action(for: .page(slug: "optics"), resume: nil, quickMinutes: 5).get(), .load("/artifacts/optics"))
        let address = try XCTUnwrap(URL(string: "https://arxiv.org/abs/1706.03762?v=2&x=1"))
        XCTAssertEqual(try AppModel.action(for: .add(address), resume: nil, quickMinutes: 5).get(),
                       .load("/add?url=https%3A%2F%2Farxiv.org%2Fabs%2F1706.03762%3Fv%3D2%26x%3D1"))
    }

    func testContinueWithoutAPageOrToAnotherAddressIsRefused() {
        guard case .failure(let none) = AppModel.action(for: .continueStudying, resume: nil, quickMinutes: 5) else {
            return XCTFail("continued without a page")
        }
        XCTAssertTrue(none.message.contains("no page to continue"))
        let away = PhoneSummary.Resume(slug: "x", title: "X", target: "https://evil.example/artifacts/x")
        guard case .failure = AppModel.action(for: .continueStudying, resume: away, quickMinutes: 5) else {
            return XCTFail("followed an outside address")
        }
    }
}

final class IntentTextTests: XCTestCase {
    private func moment(_ state: ReviewState, stale: Bool = false) -> WidgetDisplay {
        let zone = TimeZone(identifier: "Europe/Stockholm") ?? .gmt
        return .reviews(ReviewMoment(date: date("2026-11-26T15:00:00+00:00"), state: state, goal: nil, stale: stale,
                                     asOf: date("2026-11-26T09:00:00+00:00"), source: .server, zone: zone), continueTitle: nil)
    }

    func testReviewsDueAnswers() throws {
        XCTAssertEqual(try IntentText.reviewsDue(moment(.due(7, atLeast: false))).get(),
                       .init(count: 7, dialog: "7 reviews are due in Airistotle."))
        XCTAssertEqual(try IntentText.reviewsDue(moment(.due(1, atLeast: false))).get().dialog, "1 review is due in Airistotle.")
        XCTAssertEqual(try IntentText.reviewsDue(moment(.due(20, atLeast: true))).get().dialog,
                       "At least 20 reviews are due in Airistotle.")
        XCTAssertEqual(try IntentText.reviewsDue(moment(.nothingDue(next: nil))).get(),
                       .init(count: 0, dialog: "No reviews are due, and none is scheduled."))
        XCTAssertEqual(try IntentText.reviewsDue(moment(.doneToday(next: nil))).get().dialog,
                       "No reviews are due. You are done for today: nothing else is scheduled.")
        let next = try IntentText.reviewsDue(moment(.nothingDue(next: date("2026-11-27T08:00:00+00:00")))).get().dialog
        XCTAssertTrue(next.hasPrefix("No reviews are due. The next is due tomorrow "), next)
    }

    func testStaleNumbersSayHowOld() throws {
        let dialog = try IntentText.reviewsDue(moment(.due(3, atLeast: false), stale: true)).get().dialog
        XCTAssertTrue(dialog.hasPrefix("3 reviews are due in Airistotle. That is as of "), dialog)
        XCTAssertTrue(dialog.hasSuffix(", when Airistotle last updated."), dialog)
    }

    func testSignedOutAndMissingDataFailClearly() {
        XCTAssertEqual(IntentText.reviewsDue(.signedOut),
                       .failure(.init(message: "You are not signed in to Airistotle. Open the app and sign in first.")))
        guard case .failure(let none) = IntentText.reviewsDue(.noData), case .failure(let broken) = IntentText.reviewsDue(.unavailable("x"))
        else { return XCTFail("answered without data") }
        XCTAssertTrue(none.message.contains("has not loaded your reviews"))
        XCTAssertTrue(broken.message.contains("cannot read"))
    }
}

@MainActor
final class SpotlightTests: XCTestCase {
    private func page(_ slug: String, _ title: String) throws -> SavedPageSummary {
        let record = SavedPageRecord(slug: slug, title: title, savedAt: Date(), version: "1", serverVersion: nil, supersededBy: nil,
                                     deletedOnServer: false, bytes: 0, files: [:])
        return SavedPageSummary(SavedPage(record: record, bundle: try OfflineFixture.studyBundle(), pending: [],
                                          highlights: SavedHighlights(list: nil)))
    }

    func testIdentifiersCarryTheAccountAndThePage() {
        let id = SpotlightEntry.identifier(namespace: "abc123", slug: "waves")
        XCTAssertEqual(id, "page|abc123|waves")
        XCTAssertEqual(SpotlightEntry.parse(id)?.namespace, "abc123")
        XCTAssertEqual(SpotlightEntry.parse(id)?.slug, "waves")
        XCTAssertNil(SpotlightEntry.parse("page||waves"))
        XCTAssertNil(SpotlightEntry.parse("other|a|b"))
    }

    func testThePlanAddsChangesAndRemoves() {
        let kept = SpotlightEntry(id: "page|n|a", title: "A", detail: "d")
        let renamed = SpotlightEntry(id: "page|n|b", title: "B2", detail: "d")
        let gone = SpotlightEntry(id: "page|n|c", title: "C", detail: "d")
        let new = SpotlightEntry(id: "page|n|d", title: "D", detail: "d")
        let indexed = [kept.id: kept, renamed.id: SpotlightEntry(id: renamed.id, title: "B", detail: "d"), gone.id: gone]
        let (upsert, remove) = SpotlightPlan.diff(indexed: indexed, wanted: [kept, renamed, new])
        XCTAssertEqual(upsert, [renamed, new])
        XCTAssertEqual(remove, ["page|n|c"])
    }

    func testTheBookFollowsTheSavedPagesAndSignOutEmptiesIt() async throws {
        let index = FakeSpotlight()
        let defaults = try XCTUnwrap(UserDefaults(suiteName: "SpotlightTests-\(UUID().uuidString)"))
        let book = SpotlightBook(index: index, defaults: defaults)
        await book.update(namespace: "n", pages: [try page("waves", "Waves"), try page("optics", "Optics")])
        XCTAssertEqual(Set(index.entries.keys), ["page|n|waves", "page|n|optics"])
        XCTAssertEqual(index.entries["page|n|waves"]?.title, "Waves")
        XCTAssertTrue(index.entries["page|n|waves"]?.detail.contains("saved for offline") == true)
        await book.update(namespace: "n", pages: [try page("waves", "Waves")])
        XCTAssertEqual(Set(index.entries.keys), ["page|n|waves"], "a removed page leaves the index")
        // Another account: only its pages.
        await book.update(namespace: "m", pages: [try page("waves", "Waves")])
        XCTAssertEqual(Set(index.entries.keys), ["page|m|waves"])
        await book.removeAll()
        XCTAssertTrue(index.entries.isEmpty)
        XCTAssertTrue(book.indexed.isEmpty)
        XCTAssertNil(book.problem)
    }
}

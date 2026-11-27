import XCTest

/// End-to-end runs of the app against a real, throwaway Teacher server. They skip unless the
/// runner is given the server (xcodebuild passes `TEST_RUNNER_<NAME>` to the tests as `<NAME>`):
///
///   E2E_SERVER        e.g. http://127.0.0.1:8031 (a server with accounts)
///   E2E_EMAIL, E2E_PASSWORD, E2E_ACCESS_CODE   throwaway credentials for that server
///   E2E_OPEN_SERVER   a server without accounts (testOpenServer)
///   E2E_SHOTS         a folder to save screenshots in (they are also attached to the results)
///
/// Each test is one scene, run on its own with `-only-testing:` while the server is in the
/// state it needs (running, stopped, ...): see the test names.
@MainActor
final class E2ETests: XCTestCase {
    private var app: XCUIApplication!
    private let env = ProcessInfo.processInfo.environment

    override func setUp() async throws {
        continueAfterFailure = false
    }

    private func value(_ name: String) throws -> String {
        guard let value = env[name], !value.isEmpty else { throw XCTSkip("\(name) is not set: no end-to-end server") }
        return value
    }

    private func launch(reset: Bool) {
        app = XCUIApplication()
        if reset { app.launchArguments.append("-AiristotleResetForUITests") }
        app.launch()
    }

    private func shot(_ name: String) {
        let screenshot = XCUIScreen.main.screenshot()
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        if let folder = env["E2E_SHOTS"] {
            try? FileManager.default.createDirectory(atPath: folder, withIntermediateDirectories: true)
            try? screenshot.pngRepresentation.write(to: URL(fileURLWithPath: folder).appending(path: "\(name).png"))
        }
    }

    private var web: XCUIElement { app.webViews.firstMatch }

    @discardableResult
    private func waitFor(_ element: XCUIElement, _ timeout: TimeInterval = 15, _ what: String = "") -> XCUIElement {
        XCTAssertTrue(element.waitForExistence(timeout: timeout), "missing: \(what.isEmpty ? element.description : what)")
        return element
    }

    private func webLink(_ label: String) -> XCUIElement {
        web.links.matching(NSPredicate(format: "label CONTAINS %@", label)).firstMatch
    }

    private func webText(_ label: String) -> XCUIElement {
        web.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", label)).firstMatch
    }

    /// Scroll the sign-in form until `element` is on screen (its rows are made lazily).
    @discardableResult
    private func reveal(_ element: XCUIElement, timeout: TimeInterval = 15, _ what: String = "") -> XCUIElement {
        let list = app.collectionViews.firstMatch
        let deadline = Date().addingTimeInterval(timeout)
        var downs = 0
        while Date() < deadline {
            if element.exists && element.isHittable { return element }
            if downs < 3 {
                list.swipeDown(velocity: .fast)
                downs += 1
            } else {
                list.swipeUp(velocity: .slow)
            }
            usleep(300_000)
        }
        XCTFail("not on screen: \(what.isEmpty ? element.description : what)")
        return element
    }

    private func replace(_ field: XCUIElement, with text: String) {
        // Scroll the field above the keyboard, then tap at its right edge (the cursor goes to
        // the end) until it has the keyboard.
        let keyboard = app.keyboards.firstMatch
        for _ in 0..<3 where keyboard.exists && field.frame.maxY > keyboard.frame.minY - 12 {
            app.collectionViews.firstMatch.swipeUp(velocity: .slow)
        }
        for _ in 0..<4 {
            field.coordinate(withNormalizedOffset: CGVector(dx: 0.97, dy: 0.5)).tap()
            if (field.value(forKey: "hasKeyboardFocus") as? Bool) == true { break }
            usleep(400_000)
        }
        let current = (field.value as? String) ?? ""
        field.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: current.count + 2))
        field.typeText(text)
    }

    private func fillSignIn(server: String, email: String, password: String) {
        replace(reveal(app.textFields["signin.server"]), with: server)
        replace(reveal(app.textFields["signin.email"]), with: email)
        replace(reveal(app.secureTextFields["signin.password"]), with: password)
    }

    /// A button of the bottom bar, which slides away while the page is scrolled down: scroll
    /// up a little until it is back.
    private func barButton(_ identifier: String) -> XCUIElement {
        let button = app.buttons[identifier]
        for _ in 0..<4 where !(button.exists && button.isHittable) {
            web.swipeDown(velocity: .slow)
            usleep(400_000)
        }
        return button
    }

    /// Back, as the edge swipe does it (the bar has no Back button).
    private func swipeBack() {
        let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.01, dy: 0.5))
        start.press(forDuration: 0.05, thenDragTo: app.coordinate(withNormalizedOffset: CGVector(dx: 0.8, dy: 0.5)))
    }

    private func scrollTo(_ element: XCUIElement) {
        for _ in 0..<12 where !(element.exists && element.isHittable) { web.swipeUp(velocity: .slow) }
    }

    @discardableResult
    private func answerGate(option: String, required: Bool = true) -> Bool {
        // WebKit exposes the radio inputs as plain elements; an answered gate repeats the label.
        let choice = last(web.staticTexts, option)
        guard choice.waitForExistence(timeout: 10) else {
            if required { XCTFail("the gate's option \(option)") }
            return false
        }
        scrollTo(choice)
        choice.tap()
        last(web.staticTexts, "Fairly sure").tap()
        let submit = last(web.buttons, "Submit answer")
        scrollTo(submit)
        submit.tap()
        sleep(2)
        return true
    }

    // MARK: Scenes

    /// Server with accounts, running. Register, browse, answer a gate, stream a tutor reply,
    /// open the graph, sign out, sign in again.
    func test1Online() throws {
        let server = try value("E2E_SERVER")
        let email = try value("E2E_EMAIL")
        let password = try value("E2E_PASSWORD")
        let code = try value("E2E_ACCESS_CODE")
        launch(reset: true)

        waitFor(app.buttons["signin.submit"])
        shot("01-sign-in")

        // A server with accounts refuses "connect without an account", in its own words.
        replace(reveal(app.textFields["signin.server"]), with: server)
        reveal(app.buttons["signin.open"]).tap()
        reveal(app.staticTexts["signin.error"], timeout: 15, "the refusal")
        shot("02-open-refused")

        // The fields are filled in "Sign in" mode first: in "Create account" mode the password
        // field offers a strong password, whose overlay takes the keyboard from the test.
        fillSignIn(server: server, email: email, password: password)
        reveal(app.segmentedControls.buttons["Create account"]).tap()
        replace(reveal(app.secureTextFields["signin.accessCode"]), with: "wrong-code")
        reveal(app.buttons["signin.submit"]).tap()
        reveal(app.staticTexts["signin.error"], timeout: 15, "wrong access code")
        shot("03-register-wrong-code")
        replace(reveal(app.secureTextFields["signin.accessCode"]), with: code)
        reveal(app.buttons["signin.submit"]).tap()

        waitFor(web, 20, "the web view")
        waitFor(webLink("Library"), 20, "the header's Library link")
        sleep(2)
        shot("04-home")

        webLink("Library").tap()
        waitFor(webLink("Attention Is All You Need"), 20, "the study page in the library")
        sleep(1)
        shot("05-library")

        let study = webLink("Study: Attention")
        (study.exists ? study : webLink("Attention Is All You Need")).tap()
        sleep(3)
        shot("06-study-page")
        web.swipeUp()
        sleep(1)
        shot("07-study-page-gate")
    }

    /// Continues test1: answer the gate, the tutor, the graph, settings, sign out and in.
    func test2Session() throws {
        let server = try value("E2E_SERVER")
        let email = try value("E2E_EMAIL")
        let password = try value("E2E_PASSWORD")
        launch(reset: false)
        waitFor(web, 20, "the web view (still signed in)")
        waitFor(webLink("Library"), 20)
        webLink("Library").tap()
        waitFor(webText("1 source"), 20, "the library page")
        webLink("Study: Attention").tap()
        waitFor(webText("checkpoints cleared"), 20, "the study page")
        sleep(2)

        // The multiple-choice gate: an option, how sure, submit.
        answerGate(option: "An unrelated text")
        shot("08-gate-answered")

        // The tutor streams its reply.
        let tutor = web.buttons["Tutor"]
        if tutor.waitForExistence(timeout: 5) {
            tutor.tap()
            let input = waitFor(web.textViews.firstMatch, 10, "the tutor's input")
            input.tap()
            input.typeText("What does attention do?")
            web.buttons["Send"].tap()
            sleep(3)
            shot("09-tutor-streaming")
            sleep(12)
            shot("10-tutor-done")
            // The tutor panel covers the page on a phone: close it (the button reads Close or Hide).
            for label in ["Close the tutor", "Close", "Hide"] where web.buttons[label].exists {
                web.buttons[label].tap()
                break
            }
            sleep(1)
        } else {
            shot("09-no-tutor-button")
        }

        // The concept graph.
        webLink("Home").tap()
        waitFor(webLink("Concepts"), 20)
        webLink("Concepts").tap()
        sleep(4)
        shot("11-graph")

        // Back, with the edge swipe.
        swipeBack()
        sleep(2)
        shot("12-after-back")

        // Settings, then sign out.
        barButton("browser.settings").tap()
        waitFor(app.staticTexts["Signed in as"], 10)
        sleep(1)
        shot("13-settings")
        app.buttons["settings.signOut"].tap()
        let confirm = app.sheets.buttons["Sign out"].exists ? app.sheets.buttons["Sign out"] : app.buttons["Sign out"].firstMatch
        waitFor(confirm, 5).tap()
        waitFor(app.buttons["signin.submit"], 15, "the sign-in screen after signing out")
        shot("14-signed-out")

        fillSignIn(server: server, email: email, password: "not-the-password")
        reveal(app.buttons["signin.submit"]).tap()
        reveal(app.staticTexts["signin.error"], timeout: 15, "wrong password")
        shot("15-wrong-password")
        replace(reveal(app.secureTextFields["signin.password"]), with: password)
        reveal(app.buttons["signin.submit"]).tap()
        waitFor(webLink("Library"), 20, "home after signing in again")
        sleep(1)
        shot("16-signed-in-again")
    }

    /// The server is stopped. The app shows the offline banner, saved copies, and explicit
    /// errors for what was never saved and for changes.
    func test3Offline() throws {
        _ = try value("E2E_SERVER")
        launch(reset: false)
        waitFor(app.descendants(matching: .any)["banner.offline"], 20, "the offline banner")
        sleep(2)
        shot("17-offline-home")
        waitFor(webLink("Library"), 20).tap()
        sleep(2)
        shot("18-offline-library-saved-copy")
        let study = webLink("Study: Attention")
        if study.waitForExistence(timeout: 10) {
            study.tap()
            sleep(3)
            shot("19-offline-study-page")
            if !answerGate(option: "None at all", required: false) {
                // The gate was answered online: try the tutor instead, which also posts.
                let tutor = web.buttons["Tutor"]
                if tutor.waitForExistence(timeout: 5) {
                    tutor.tap()
                    let input = waitFor(web.textViews.firstMatch, 10, "the tutor's input")
                    input.tap()
                    input.typeText("Are you there?")
                    web.buttons["Send"].tap()
                    sleep(2)
                }
            }
            shot("20-offline-change-refused")
        }
        webLink("Home").tap()
        sleep(2)
        waitFor(webLink("History"), 10).tap()
        sleep(2)
        shot("21-offline-never-saved")
        barButton("browser.settings").tap()
        sleep(1)
        shot("22-offline-settings")
    }

    /// The server is running again but the session was revoked on the server: the app goes
    /// back to its sign-in screen and says why.
    func test4SessionRevoked() throws {
        _ = try value("E2E_SERVER")
        launch(reset: false)
        waitFor(app.buttons["signin.submit"], 25, "the sign-in screen after the server refused the session")
        sleep(1)
        shot("23-session-revoked")
    }

    /// A server without accounts: signing in is refused, connecting without one works.
    func test5OpenServer() throws {
        let server = try value("E2E_OPEN_SERVER")
        launch(reset: true)
        fillSignIn(server: server, email: "reader@example.test", password: "whatever")
        reveal(app.buttons["signin.submit"]).tap()
        reveal(app.staticTexts["signin.error"], timeout: 15, "no accounts on this server")
        shot("24-open-server-sign-in-refused")
        reveal(app.buttons["signin.open"]).tap()
        waitFor(webLink("Library"), 20, "home on the open server")
        sleep(2)
        shot("25-open-server-home")
        barButton("browser.settings").tap()
        sleep(1)
        shot("26-open-server-settings")
    }

    /// Server with accounts, running: the Add page, a download, a lab.
    func test6Extras() throws {
        let server = try value("E2E_SERVER")
        launch(reset: true)
        fillSignIn(server: server, email: try value("E2E_EMAIL"), password: try value("E2E_PASSWORD"))
        reveal(app.buttons["signin.submit"]).tap()
        waitFor(webLink("Library"), 20)

        webLink("Add").tap()
        sleep(3)
        shot("27-add-page")

        webLink("Home").tap()
        waitFor(webLink("History"), 20).tap()
        let download = webLink("Download JSON")
        waitFor(download, 15)
        scrollTo(download)
        download.tap()
        sleep(3)
        shot("28-download-share-sheet")
        if app.buttons["Close"].exists { app.buttons["Close"].tap() } else { app.swipeDown() }
        sleep(1)

        webLink("Home").tap()
        waitFor(webLink("Library"), 20).tap()
        waitFor(webText("1 source"), 20, "the library page")
        webLink("Study: Attention").tap()
        waitFor(webText("checkpoints cleared"), 20, "the study page")
        let lab = web.links.matching(NSPredicate(format: "label CONTAINS 'from scratch'")).firstMatch
        if !lab.exists {
            let create = web.buttons["Create a lab"]
            scrollTo(create)
            create.tap()
            sleep(2)
            // The model picker's own confirm button, when the page shows one.
            let confirm = web.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Create' AND label != 'Create a lab'")).firstMatch
            if confirm.exists { tapScrolled(confirm) }
            sleep(6) // the web client opens the new lab by itself
        } else {
            scrollTo(lab)
            lab.tap()
        }
        waitFor(webText("Python"), 25, "the lab page")
        sleep(2)
        shot("30-lab-page")
    }

    /// Signed in, server running: the web header's own "Sign out" button is hidden in the app
    /// (Settings signs out); the header has its links.
    func test7WebSignOutHidden() throws {
        _ = try value("E2E_SERVER")
        launch(reset: false)
        waitFor(webLink("Library"), 20, "the header's links")
        sleep(1)
        XCTAssertFalse(web.buttons["Sign out"].exists && web.buttons["Sign out"].isHittable, "the header's Sign out is hidden")
        shot("31-web-sign-out-hidden")
    }

    /// Server with accounts, running: the header without its Sign out, the bar hiding and
    /// coming back with the scrolling, Reload in the menu, the largest text size, and the study
    /// page reopened where it was left. Screenshots are tagged with E2E_APPEARANCE (set the
    /// simulator's appearance first: `xcrun simctl ui <device> appearance dark`).
    func test11PhoneReading() throws {
        let server = try value("E2E_SERVER")
        let tag = env["E2E_APPEARANCE"] ?? "light"
        launch(reset: true)
        fillSignIn(server: server, email: try value("E2E_EMAIL"), password: try value("E2E_PASSWORD"))
        reveal(app.buttons["signin.submit"]).tap()
        waitFor(webLink("Library"), 20)
        sleep(1)
        XCTAssertFalse(web.buttons["Sign out"].exists && web.buttons["Sign out"].isHittable, "the header's Sign out is hidden")
        shot("60-\(tag)-home")

        openStudyPage()
        XCTAssertTrue(app.buttons["browser.settings"].isHittable)
        XCTAssertFalse(app.buttons["browser.back"].exists, "no Back button: the edge swipe goes back")
        shot("61-\(tag)-study-top")
        web.swipeUp(velocity: .slow)
        web.swipeUp(velocity: .slow)
        sleep(1)
        XCTAssertFalse(app.buttons["browser.settings"].isHittable, "the bar slid away while scrolling down")
        shot("62-\(tag)-study-scrolled-bar-hidden")
        web.swipeDown(velocity: .slow)
        sleep(1)
        XCTAssertTrue(app.buttons["browser.settings"].isHittable, "the bar is back on scrolling up")
        shot("63-\(tag)-study-bar-back")

        barButton("browser.more").tap()
        waitFor(app.buttons["Reload"], 5, "Reload in the menu")
        shot("64-\(tag)-more-menu")
        app.buttons["Reload"].tap()
        waitFor(webText("checkpoints cleared"), 20, "the study page reloaded")

        barButton("browser.settings").tap()
        let larger = waitFor(app.buttons["settings.textSize-Increment"], 10, "the text size stepper")
        for _ in 0..<6 { larger.tap() }
        XCTAssertEqual(larger.value as? String, "150 percent", "the largest size")
        shot("65-\(tag)-settings-reading")
        app.buttons["settings.done"].tap()
        sleep(2)
        shot("66-\(tag)-study-text-150")
        web.swipeUp(velocity: .slow)
        sleep(1)
        shot("67-\(tag)-study-text-150-scrolled")

        // Quit and come back: the same page, scrolled where it was.
        sleep(1)
        app.terminate()
        launch(reset: false)
        waitFor(webText("checkpoints cleared"), 25, "the study page reopened")
        sleep(3)
        shot("68-\(tag)-reopened")

        barButton("browser.settings").tap()
        let smaller = waitFor(app.buttons["settings.textSize-Decrement"], 10)
        for _ in 0..<6 { smaller.tap() }
        app.buttons["settings.done"].tap()
    }

    // MARK: Phase 2: saved pages, grading on the phone, sync

    /// The last on-screen element with exactly `label` (gates repeat labels: an answered gate's
    /// confidence choices stay on the page).
    private func last(_ query: XCUIElementQuery, _ label: String) -> XCUIElement {
        let matches = query.matching(NSPredicate(format: "label == %@", label)).allElementsBoundByIndex
        return matches.max { $0.frame.minY < $1.frame.minY } ?? query[label]
    }

    /// The option `label` of the gate whose other option `sibling` is shown (the page repeats
    /// the source's title elsewhere).
    private func option(_ label: String, near sibling: String) -> XCUIElement {
        let anchor = web.staticTexts[sibling]
        let candidates = web.staticTexts.matching(NSPredicate(format: "label == %@", label)).allElementsBoundByIndex
        return candidates.filter { $0.frame.minY < anchor.frame.minY && anchor.frame.minY - $0.frame.minY < 120 }.first
            ?? candidates.last ?? web.staticTexts[label]
    }

    private func tapScrolled(_ element: XCUIElement) {
        scrollTo(element)
        element.tap()
    }

    private func openStudyPage() {
        waitFor(webLink("Library"), 20).tap()
        waitFor(webText("1 source"), 20, "the library page")
        webLink("Study: Attention").tap()
        waitFor(webText("checkpoints cleared"), 20, "the study page")
        sleep(2)
    }

    /// Server with accounts, running: register, save the study page, look at the Saved list and
    /// Settings, then clear the response cache (saved pages must not depend on it).
    func test8SaveForOffline() throws {
        let server = try value("E2E_SERVER")
        launch(reset: true)
        fillSignIn(server: server, email: try value("E2E_EMAIL"), password: try value("E2E_PASSWORD"))
        reveal(app.segmentedControls.buttons["Create account"]).tap()
        replace(reveal(app.secureTextFields["signin.accessCode"]), with: try value("E2E_ACCESS_CODE"))
        reveal(app.buttons["signin.submit"]).tap()
        openStudyPage()
        waitFor(app.buttons["browser.save"], 10, "Save for offline").tap()
        waitFor(app.buttons["browser.savedState"], 90, "the saved state")
        shot("40-saved-for-offline")
        barButton("browser.saved").tap()
        waitFor(app.staticTexts["Saved for offline"], 10)
        sleep(1)
        shot("41-saved-list")
        app.buttons["saved.done"].tap()
        barButton("browser.settings").tap()
        let model = waitFor(app.staticTexts["settings.model"].exists ? app.staticTexts["settings.model"]
                            : app.descendants(matching: .any)["settings.model"], 10, "the model line")
        scrollTo(model)
        sleep(1)
        shot("42-settings-offline")
        let clear = app.buttons["settings.clearCache"]
        for _ in 0..<4 where !clear.isHittable { app.collectionViews.firstMatch.swipeUp() }
        clear.tap()
        waitFor(app.buttons["Clear saved copies"].firstMatch, 5).tap()
        sleep(1)
        shot("43-cache-cleared")
        app.buttons["settings.done"].tap()
    }

    /// The server is stopped: cold start, open the saved page, clear its gates on the phone
    /// (multiple choice, a written answer graded by the on-device model, the prediction).
    func test9OfflineSavedPage() throws {
        _ = try value("E2E_SERVER")
        launch(reset: false)
        waitFor(app.descendants(matching: .any)["banner.offline"], 25, "the offline banner")
        sleep(1)
        shot("44-offline-cold-start")
        barButton("browser.saved").tap()
        waitFor(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'saved.row.'")).firstMatch, 10).tap()
        waitFor(webText("checkpoints cleared"), 25, "the saved study page")
        sleep(2)
        shot("45-saved-page-offline")

        answerGate(option: "An unrelated text")
        shot("46-offline-wrong-answer")
        tapScrolled(option("Attention Is All You Need", near: "An unrelated text"))
        tapScrolled(last(web.staticTexts, "Certain"))
        tapScrolled(last(web.buttons, "Submit answer"))
        sleep(2)
        shot("47-offline-gate-passed")

        let answer = web.textViews.firstMatch
        waitFor(answer, 10, "the written answer box")
        scrollTo(answer)
        answer.tap()
        answer.typeText("The main idea of the paper is that a model built only from attention can replace recurrent "
            + "networks. As evidence it reached better translation scores while training much faster than older models.")
        tapScrolled(last(web.staticTexts, "Fairly sure"))
        tapScrolled(last(web.buttons, "Submit answer"))
        sleep(25) // the on-device model grades it
        shot("48-offline-written-answer-graded")

        let guess = web.staticTexts["It ends with a short critical note"]
        if guess.waitForExistence(timeout: 5) {
            tapScrolled(guess)
            tapScrolled(last(web.buttons, "Commit my guess"))
            sleep(2)
        }
        shot("49-offline-prediction")
        barButton("browser.saved").tap()
        waitFor(app.staticTexts["Saved for offline"], 10)
        sleep(1)
        shot("50-saved-list-waiting")
        app.buttons["saved.done"].tap()
    }

    /// The server is running again (and a gate was answered there meanwhile): the app syncs on
    /// launch; the rejection shows in the sync log.
    func test10Sync() throws {
        _ = try value("E2E_SERVER")
        launch(reset: false)
        waitFor(webLink("Library"), 25, "home online")
        sleep(4) // the sync at launch
        barButton("browser.saved").tap()
        waitFor(app.staticTexts["Saved for offline"], 10)
        sleep(2)
        shot("51-after-sync-log")
        app.swipeUp()
        sleep(1)
        shot("52-after-sync-log-more")
        app.buttons["saved.done"].tap()
        openStudyPage()
        sleep(2)
        shot("53-study-page-after-sync")
    }

    // MARK: Review, automatic saving and highlights offline (a server without accounts)

    /// Launch straight onto `E2E_REVIEW_SERVER` (a throwaway server without accounts whose review
    /// questions are due), past the sign-in screen.
    private func launchConnected(review: String? = nil, open target: String? = nil, reset: Bool = true,
                                 extra: [String] = []) throws {
        let server = try value("E2E_REVIEW_SERVER")
        app = XCUIApplication()
        if reset { app.launchArguments += ["-AiristotleResetForUITests", "-AiristotleConnectForUITests", server] }
        if let review { app.launchArguments += ["-AiristotleReview", review] }
        if let target { app.launchArguments += ["-AiristotleOpen", target] }
        app.launchArguments += extra
        app.launch()
    }

    private func element(_ identifier: String) -> XCUIElement {
        app.descendants(matching: .any)[identifier].firstMatch
    }

    private func answerReview(option index: Int) {
        waitFor(app.buttons["review.option.\(index)"], 20, "option \(index)").tap()
        app.buttons["review.confidence.medium"].tap()
        app.buttons["review.submit"].tap()
        waitFor(element("review.verdict"), 20, "the verdict")
    }

    /// A review online: a multiple choice with maths, its verdict, a written answer with maths,
    /// then the summary.
    func test12ReviewSession() throws {
        try launchConnected(review: "all")
        waitFor(app.buttons["review.option.0"], 30, "the first question")
        sleep(2) // the maths renderer
        shot("60-review-choice")
        answerReview(option: 3)
        sleep(3)
        shot("61-review-verdict")
        var seenBlurb = false
        for _ in 0..<6 {
            let next = app.buttons["review.next"]
            guard next.waitForExistence(timeout: 10) else { break }
            next.tap()
            if element("review.summary").waitForExistence(timeout: 3) { break }
            let text = app.textViews["review.text"]
            if text.waitForExistence(timeout: 8) {
                sleep(2)
                if !seenBlurb { shot("62-review-blurb") }
                text.tap()
                text.typeText("The energy is the mass times c squared, and c squared is a huge number so a little mass gives a lot.")
                app.buttons["review.keyboardDone"].firstMatch.tap()
                if !seenBlurb { shot("63-review-blurb-typed") }
                app.buttons["review.confidence.high"].tap()
                app.buttons["review.submit"].tap()
                waitFor(element("review.verdict"), 20, "the written answer's verdict")
                sleep(3) // the maths renderer
                if !seenBlurb { shot("64-review-blurb-verdict") }
                seenBlurb = true
            } else {
                answerReview(option: 0)
            }
        }
        waitFor(element("review.summary"), 20, "the summary")
        sleep(1)
        shot("65-review-summary")
        app.buttons["review.done"].tap()
    }

    /// A quick review: the time left shows; nothing due says when the next is.
    func test13QuickReview() throws {
        try launchConnected(review: "quick")
        waitFor(element("review.timer"), 30, "the timer")
        sleep(2)
        shot("66-quick-review-timer")
        app.buttons["review.close"].tap()
    }

    /// The page being studied is saved automatically; the Saved list shows it and the storage.
    func test14AutomaticSave() throws {
        try launchConnected(open: "/artifacts/waves")
        waitFor(webText("Waves"), 30, "the study page")
        sleep(8) // the automatic save
        barButton("browser.saved").tap()
        waitFor(app.staticTexts["Saved for offline"], 10)
        sleep(2)
        shot("67-saved-automatic")
        app.swipeUp()
        sleep(1)
        shot("68-saved-storage")
        app.buttons["saved.done"].tap()
    }

    /// The server is stopped: the saved page opens, a passage is highlighted from the web
    /// popover and the highlight is kept on the phone.
    func test15HighlightOffline() throws {
        _ = try value("E2E_REVIEW_SERVER")
        app = XCUIApplication()
        app.launch()
        let passage = webText("superposition")
        waitFor(passage, 30, "the saved page")
        sleep(2)
        passage.press(forDuration: 1.2)
        sleep(2)
        shot("69-highlight-selection")
        let highlight = web.buttons["Highlight"].firstMatch
        if highlight.waitForExistence(timeout: 5) {
            highlight.tap()
            sleep(2)
        }
        shot("70-highlight-made")
        // Tapping the highlight offers Add note and Remove; the note box gets the keyboard.
        let mark = web.staticTexts.matching(NSPredicate(format: "label == %@", "up:")).firstMatch
        if mark.waitForExistence(timeout: 5) {
            mark.tap()
            sleep(2)
            shot("71-highlight-popover")
            let addNote = web.buttons["Add note"].firstMatch
            if addNote.waitForExistence(timeout: 5) {
                addNote.tap()
                sleep(2)
                app.typeText("Superposition: the waves add up.")
                sleep(1)
                shot("72-highlight-note-keyboard")
                let save = web.buttons["Save note"].firstMatch
                if save.exists { save.tap() }
                sleep(2)
                shot("73-highlight-note-saved")
            }
        }
        barButton("browser.saved").tap()
        waitFor(app.staticTexts["Saved for offline"], 10)
        sleep(1)
        shot("74-saved-highlight-waiting")
        app.buttons["saved.done"].tap()
    }

    /// The server is stopped: the review runs on the saved questions, graded on the phone.
    func test16ReviewOffline() throws {
        _ = try value("E2E_REVIEW_SERVER")
        app = XCUIApplication()
        app.launch()
        sleep(4)
        barButton("browser.review").tap()
        app.buttons["browser.reviewAll"].tap()
        waitFor(element("review.offline"), 30, "the offline banner")
        sleep(2)
        shot("75-review-offline")
        guard app.buttons["review.option.0"].waitForExistence(timeout: 5) else { return }
        answerReview(option: 0)
        sleep(3)
        shot("76-review-offline-verdict")
        for _ in 0..<4 {
            let next = app.buttons["review.next"]
            guard next.waitForExistence(timeout: 5) else { break }
            next.tap()
            if element("review.summary").waitForExistence(timeout: 3) { break }
            let text = app.textViews["review.text"]
            if text.waitForExistence(timeout: 5) {
                sleep(2)
                text.tap()
                text.typeText("The energy is the mass times c squared, and c squared is a huge number so a little mass gives a lot.")
                app.buttons["review.keyboardDone"].firstMatch.tap()
                app.buttons["review.confidence.medium"].tap()
                app.buttons["review.submit"].tap()
                sleep(4)
                shot("77-review-offline-blurb")
                if app.buttons["review.skip"].exists { app.buttons["review.skip"].tap() }
            } else if app.buttons["review.option.0"].exists {
                answerReview(option: 0)
            }
        }
        if element("review.summary").waitForExistence(timeout: 10) {
            sleep(1)
            shot("78-review-offline-summary")
        }
    }

    /// The server is back: what was answered and highlighted offline uploads; the Saved list
    /// says what the server did not take.
    func test17SyncAfterOffline() throws {
        _ = try value("E2E_REVIEW_SERVER")
        app = XCUIApplication()
        app.launch()
        sleep(8) // the sync at launch
        barButton("browser.saved").tap()
        waitFor(app.staticTexts["Saved for offline"], 10)
        sleep(2)
        shot("79-saved-after-sync")
        app.swipeUp()
        sleep(1)
        shot("80-saved-after-sync-log")
        app.buttons["saved.done"].tap()
    }

    // MARK: Reminders, daily goal, links (a server without accounts, running, questions due)

    private var springboard: XCUIApplication { XCUIApplication(bundleIdentifier: "com.apple.springboard") }

    /// The system's notification prompt: allow.
    private func allowNotifications() {
        let allow = springboard.alerts.buttons["Allow"]
        if allow.waitForExistence(timeout: 8) { allow.tap() }
    }

    /// A Settings row, scrolled to.
    private func settingsRow(_ identifier: String) -> XCUIElement {
        let row = element(identifier)
        for _ in 0..<10 where !(row.exists && row.isHittable) { app.collectionViews.firstMatch.swipeUp(velocity: .slow) }
        return row
    }

    /// Settings: the daily goal and the reminders; turning reminders on asks for permission and
    /// then offers the time.
    func test18HabitSettings() throws {
        try launchConnected()
        waitFor(webText("Mass and energy"), 30, "Home")
        sleep(3)
        barButton("browser.settings").tap()
        waitFor(settingsRow("settings.dailyGoal"), 10, "the daily goal")
        sleep(1)
        shot("81-settings-daily-goal")
        let reminders = settingsRow("settings.reminders")
        waitFor(reminders, 10, "the reminders toggle")
        XCTAssertEqual(reminders.value as? String, "0", "reminders are off by default")
        XCTAssertEqual(settingsRow("settings.badge").value as? String, "0", "the badge is off by default")
        reminders.switches.firstMatch.tap()
        allowNotifications()
        // Bring the section up (a fixed drag: the list does not scroll a row to a given place).
        app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.85))
            .press(forDuration: 0.1, thenDragTo: app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.45)))
        waitFor(element("settings.reminderTime"), 10, "the time picker")
        waitFor(element("settings.nextReminder"), 5)
        XCTAssertEqual(element("settings.reminders").value as? String, "1")
        sleep(2)
        shot("82-settings-reminders")
        app.buttons["settings.done"].tap()
    }

    /// The review menu's progress line opens the progress view.
    func test19Progress() throws {
        try launchConnected()
        waitFor(webText("Mass and energy"), 30, "Home")
        sleep(4)
        barButton("browser.review").tap()
        waitFor(app.buttons["browser.progress"], 10, "the progress line").tap()
        waitFor(element("progress.calendar"), 10, "the calendar")
        sleep(1)
        shot("83-progress")
        app.buttons["progress.done"].tap()
    }

    /// The app's links: the review opens; an unknown link is refused with a message; the Add
    /// link fills in the address without fetching it.
    func test20DeepLinks() throws {
        try launchConnected()
        waitFor(webText("Mass and energy"), 30, "Home")
        sleep(3)
        openLink("airistotle://review")
        // A question, or (when all were answered) the review saying nothing is due.
        let opened = app.buttons["review.option.0"].waitForExistence(timeout: 30) || element("review.nothingDue").exists
        XCTAssertTrue(opened, "the review from a link")
        shot("84-link-review")
        app.buttons["review.close"].tap()
        sleep(2)
        openLink("airistotle://somewhere-else")
        waitFor(app.alerts["Link not opened"], 15, "the refusal")
        shot("85-link-refused")
        app.alerts["Link not opened"].buttons["OK"].firstMatch.tap()
        openLink("airistotle://add?url=https%3A%2F%2Farxiv.org%2Fabs%2F1706.03762")
        let field = web.textFields.matching(NSPredicate(format: "value CONTAINS %@", "arxiv.org/abs/1706.03762")).firstMatch
        waitFor(field, 30, "the Add page with the address filled in")
        sleep(1)
        shot("86-link-add")
        openLink("airistotle://page/optics")
        waitFor(webText("Refraction"), 30, "the study page from a link")
    }

    /// Open a link as another app would (the system may ask to confirm).
    private func openLink(_ text: String) {
        guard let url = URL(string: text) else { return XCTFail(text) }
        XCUIDevice.shared.system.open(url)
        let confirm = springboard.buttons["Open"]
        if confirm.waitForExistence(timeout: 3) { confirm.tap() }
    }

    /// A reminder delivered (the app schedules today's a few seconds ahead), then tapped: it
    /// opens the review.
    func test21Reminder() throws {
        let server = try value("E2E_REVIEW_SERVER")
        app = XCUIApplication()
        app.launchArguments += ["-AiristotleResetForUITests", "-AiristotleConnectForUITests", server,
                                "-AiristotleReminderInSeconds", "40"]
        app.launch()
        allowNotifications()
        waitFor(webText("Mass and energy"), 30, "Home")
        XCUIDevice.shared.press(.home)
        // The reminder's own text (the home screen's widget also says "reviews due").
        let banner = springboard.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS %@", "review questions are ready")).firstMatch
        if !banner.waitForExistence(timeout: 60) { shot("87-reminder-missing") }
        waitFor(banner, 1, "the reminder")
        sleep(1)
        shot("87-reminder-delivered")
        banner.tap()
        waitFor(app.buttons["review.option.0"], 30, "the review from the reminder")
        shot("88-reminder-opened-review")
    }

    /// The real widget on the home screen (Springboard driven; best effort: the gallery's
    /// layout is the system's). The app writes the snapshot first.
    func test22HomeScreenWidget() throws {
        try launchConnected()
        waitFor(webText("Mass and energy"), 30, "Home")
        sleep(5) // the summary and the snapshot
        XCUIDevice.shared.press(.home)
        sleep(2)
        let board = springboard
        // Leftovers of an earlier run (a shortcut's answer, the home screen still being edited).
        for _ in 0..<3 where board.buttons["Done"].firstMatch.exists {
            board.buttons["Done"].firstMatch.tap()
            sleep(1)
        }
        board.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.62)).press(forDuration: 2.0)
        sleep(1)
        shot("90-widget-step-edit")
        let edit = board.buttons["Edit"].firstMatch
        if edit.waitForExistence(timeout: 5) { edit.tap() }
        let add = board.buttons.matching(NSPredicate(format: "label CONTAINS[c] %@", "Add Widget")).firstMatch
        waitFor(add, 10, "Add Widget").tap()
        sleep(2)
        shot("91-widget-step-list")
        let row = board.cells.matching(NSPredicate(format: "label CONTAINS %@", "Airistotle")).firstMatch
        if row.waitForExistence(timeout: 5) {
            row.tap()
        } else {
            // The list's first row (the apps are listed alphabetically, Airistotle first here).
            board.coordinate(withNormalizedOffset: CGVector(dx: 0.3, dy: 0.29)).tap()
        }
        sleep(3)
        shot("92-widget-gallery")
        let addButton = board.buttons.matching(NSPredicate(format: "label CONTAINS[c] %@", "Add Widget")).firstMatch
        waitFor(addButton, 10, "the gallery's Add Widget").tap()
        sleep(2)
        let done = board.buttons["Done"].firstMatch
        if done.waitForExistence(timeout: 5) { done.tap() }
        sleep(4)
        shot("93-widget-home-screen")
        // A tap opens the review.
        let widget = board.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", "reviews due")).firstMatch
        waitFor(widget, 10, "the widget").tap()
        waitFor(app.buttons["review.option.0"], 30, "the review from the widget")
        shot("94-widget-opened-review")
    }

    /// The Shortcuts app lists the app's shortcuts without any setup (best effort: the
    /// Shortcuts app's layout is the system's).
    func test23ShortcutsApp() throws {
        try launchConnected()
        waitFor(webText("Mass and energy"), 30, "Home")
        let shortcuts = XCUIApplication(bundleIdentifier: "com.apple.shortcuts")
        shortcuts.launch()
        sleep(3)
        for label in ["Continue", "Not Now", "OK"] where shortcuts.buttons[label].exists { shortcuts.buttons[label].tap() }
        // From a list ("All Shortcuts") back to the start, where the apps' shortcuts are.
        let back = shortcuts.navigationBars.buttons.firstMatch
        if back.exists, back.isHittable { back.tap() } else { shortcuts.coordinate(withNormalizedOffset: CGVector(dx: 0.09, dy: 0.095)).tap() }
        sleep(2)
        shot("95-shortcuts-start")
        let entry = shortcuts.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", "Airistotle")).firstMatch
        for _ in 0..<6 where !entry.exists { shortcuts.swipeUp(velocity: .slow) }
        waitFor(entry, 10, "Airistotle in Shortcuts").tap()
        sleep(2)
        shot("96-shortcuts-airistotle")
        // "Reviews Due" answers without opening the app.
        let due = shortcuts.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", "Reviews Due")).firstMatch
        waitFor(due, 5, "Reviews Due").tap()
        // The answer shows in a system sheet (another process than Shortcuts).
        let said = NSPredicate(format: "label CONTAINS %@", "due in Airistotle")
        let inShortcuts = shortcuts.descendants(matching: .any).matching(said).firstMatch
        let inSystem = springboard.descendants(matching: .any).matching(said).firstMatch
        let deadline = Date().addingTimeInterval(20)
        var shown = false
        while !shown && Date() < deadline {
            shown = inShortcuts.exists || inSystem.exists
            if !shown { usleep(500_000) }
        }
        shot("97-shortcut-reviews-due")
        XCTAssertTrue(shown, "the answer of Reviews Due")
        for done in [springboard.buttons["Done"].firstMatch, shortcuts.buttons["Done"].firstMatch] where done.exists {
            done.tap()
            break
        }
    }

    /// A saved page is found by the phone's search and opens in the app (best effort: the
    /// system indexes in its own time).
    func test24SpotlightSavedPage() throws {
        try launchConnected(open: "/artifacts/waves")
        waitFor(webText("Waves"), 30, "the study page")
        sleep(10) // the automatic save, then the index
        // Another page on screen, so the search result has to open this one.
        openLink("airistotle://page/optics")
        waitFor(webText("Refraction"), 30, "another page")
        XCUIDevice.shared.press(.home)
        sleep(1)
        // The home screen's Search button (or the pull-down search).
        let searchButton = springboard.descendants(matching: .any).matching(NSPredicate(format: "label == %@", "Search")).firstMatch
        if searchButton.waitForExistence(timeout: 3) { searchButton.tap() } else { springboard.swipeDown() }
        // Search runs in its own process.
        let spotlight = XCUIApplication(bundleIdentifier: "com.apple.Spotlight")
        sleep(2) // the search field has the keyboard
        if spotlight.exists { spotlight.typeText("Waves") } else { springboard.typeText("Waves") }
        let saved = NSPredicate(format: "label CONTAINS[c] %@ AND label CONTAINS[c] %@", "Waves", "saved for offline")
        let candidates = [spotlight.descendants(matching: .any).matching(saved).firstMatch,
                          springboard.descendants(matching: .any).matching(saved).firstMatch]
        var result = candidates[0]
        var found = false
        let deadline = Date().addingTimeInterval(20)
        while !found && Date() < deadline {
            if let hit = candidates.first(where: \.exists) {
                result = hit
                found = true
            } else {
                usleep(500_000)
            }
        }
        shot("98-spotlight-saved-page")
        guard found else { return XCTFail("the saved page in search") }
        result.tap()
        waitFor(web.staticTexts.matching(NSPredicate(format: "label == %@", "Waves")).firstMatch, 30, "the page opened from search")
        XCTAssertFalse(web.staticTexts.matching(NSPredicate(format: "label == %@", "Refraction")).firstMatch.exists)
        shot("99-spotlight-opened")
    }

    // MARK: Aristotle

    /// With `E2E_LARGE_TYPE` set, the app runs at a large accessibility text size (screenshots).
    private var largeType: [String] {
        env["E2E_LARGE_TYPE"].map { $0.isEmpty ? [] : ["-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityL"] }
            ?? []
    }

    private var mascot: XCUIElement { element("mascot.message") }

    /// Finishing a study page (its last checkpoint) brings Aristotle's congratulation, naming
    /// the page, once: not again when the page is opened again. A page complete when opened
    /// brings nothing.
    func test25MascotCongratulates() throws {
        try launchConnected(open: "/artifacts/superposition", extra: largeType)
        waitFor(webText("Two waves meet at one point"), 30, "the study page")
        sleep(2)
        XCTAssertFalse(mascot.exists, "nothing before an answer")
        answerGate(option: "They add up")
        waitFor(mascot, 15, "the congratulation")
        let label = mascot.label
        XCTAssertTrue(label.contains("You finished Waves and superposition."), label)
        sleep(1)
        shot("c1-congratulation\(largeType.isEmpty ? "" : "-large-type")")
        mascot.tap()
        let gone = NSPredicate(format: "exists == false")
        expectation(for: gone, evaluatedWith: mascot)
        waitForExpectations(timeout: 5)

        // Opened again, in a new launch: complete already, nothing is said.
        app.terminate()
        try launchConnected(reset: false)
        waitFor(webText("Two waves meet at one point"), 30, "the page reopened")
        sleep(6)
        XCTAssertFalse(mascot.exists, "not again for the same page")
        // A page that was complete before (answered on the server): opening it says nothing.
        app.terminate()
        try launchConnected(open: "/artifacts/optics")
        waitFor(webText("Refraction"), 30, "a finished page")
        sleep(5)
        XCTAssertFalse(mascot.exists, "already complete when opened")
    }

    /// Coming back after days away: Aristotle waves, says what is due, and offers the review.
    func test26MascotWelcomeBack() throws {
        try launchConnected(extra: ["-AiristotleLastActiveDaysAgo", "5"] + largeType)
        waitFor(webText("Mass and energy"), 30, "Home")
        waitFor(mascot, 20, "the welcome")
        let label = mascot.label
        XCTAssertTrue(label.contains("Welcome back."), label)
        XCTAssertTrue(label.contains("due") || label.contains("Pick up"), label)
        sleep(2)
        shot("c2-welcome-back\(largeType.isEmpty ? "" : "-large-type")")
        expectation(for: NSPredicate(format: "exists == false"), evaluatedWith: mascot)
        waitForExpectations(timeout: 20) // goes by itself
        guard largeType.isEmpty else { return }
        // The setting.
        barButton("browser.settings").tap()
        let toggle = settingsRow("settings.mascot")
        waitFor(toggle, 10, "Show Aristotle")
        XCTAssertEqual(toggle.value as? String, "1", "on by default")
        app.collectionViews.firstMatch.swipeUp(velocity: .slow) // its explanation into view
        sleep(1)
        shot("c3-settings-show-aristotle")
        app.buttons["settings.done"].tap()
    }

    /// The first launch after connecting: no welcome.
    func test27MascotNoWelcomeOnFirstLaunch() throws {
        try launchConnected()
        waitFor(webText("Mass and energy"), 30, "Home")
        sleep(6)
        XCTAssertFalse(mascot.exists)
    }
}

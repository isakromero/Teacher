import SwiftUI

/// The native review screen, full screen, one question at a time: the question, its answer
/// (options or a written answer), "How sure are you?", then the verdict with the feedback and
/// the answer, then the next question; a summary at the end. A quick review shows the time
/// left and ends after the question that is on screen when the time runs out.
struct ReviewSessionView: View {
    @State private var session: ReviewSession
    @State private var assets: RendererAssets
    let server: String
    /// Today's goal and the streak, for the summary.
    let habits: HabitCenter?
    /// Aristotle, who may speak over the summary (never over a question).
    let mascot: MascotCenter?
    let close: () -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @FocusState private var writing: Bool

    init(session: ReviewSession, assets: RendererAssets, server: String, habits: HabitCenter? = nil, mascot: MascotCenter? = nil,
         close: @escaping () -> Void) {
        _session = State(initialValue: session)
        _assets = State(initialValue: assets)
        self.server = server
        self.habits = habits
        self.mascot = mascot
        self.close = close
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            if case .phone(let reason) = session.source, session.phase != .loading {
                offlineBanner(reason)
            }
            Divider().overlay(Color.pageBorder)
            ScrollViewReader { proxy in
                ScrollView {
                    content
                        .padding(.horizontal, 20)
                        .padding(.vertical, 18)
                        .frame(maxWidth: 680)
                        .frame(maxWidth: .infinity)
                }
                .scrollDismissesKeyboard(.interactively)
                .onChange(of: writing) { _, focused in
                    if focused { withAnimation(reduceMotion ? nil : .default) { proxy.scrollTo("answer", anchor: .center) } }
                }
                .onChange(of: session.phase) { _, _ in proxy.scrollTo("top", anchor: .top) }
            }
            .overlay {
                if let mascot {
                    // Only messages without a button show here (`MascotMessage.places`).
                    MascotOverlay(center: mascot, place: .review, bottomInset: 8) { _ in }
                }
            }
            .safeAreaInset(edge: .bottom) { actionBar }
        }
        .background(Color.pageBackground.ignoresSafeArea())
        .environment(assets)
        .sensoryFeedback(trigger: session.lastVerdict) { _, verdict in verdict.map { Self.haptic($0.outcome) } ?? nil }
        .toolbar {
            ToolbarItemGroup(placement: .keyboard) {
                Spacer()
                Button("Done") { writing = false }.accessibilityIdentifier("review.keyboardDone")
            }
        }
        .task {
            await assets.reload()
            await session.start()
        }
        .onChange(of: mascotFree, initial: true) { _, free in mascot?.setAvailable(.review, free) }
        .onDisappear { mascot?.setAvailable(.review, false) }
    }

    /// Aristotle may speak once the review is over (its summary), never during a question.
    private var mascotFree: Bool {
        switch session.phase {
        case .summary, .nothingDue: true
        case .loading, .asking, .graded, .failed: false
        }
    }

    /// The same haptics as a checkpoint on a page (`BrowserView`).
    static func haptic(_ outcome: AnswerOutcome) -> SensoryFeedback {
        switch outcome {
        case .passed: .success
        case .partial: .warning
        case .failed: .error
        }
    }

    // MARK: Header

    private var header: some View {
        VStack(spacing: 8) {
            HStack(spacing: 12) {
                Button(action: close) {
                    Image(systemName: "xmark")
                        .font(.body.weight(.semibold))
                        .frame(width: 44, height: 44)
                        .contentShape(.rect)
                }
                .accessibilityLabel("Close review")
                .accessibilityIdentifier("review.close")
                Spacer(minLength: 0)
                VStack(spacing: 2) {
                    Text(session.isQuick ? "Quick review" : "Review").font(.headline)
                    if let progress = progressText {
                        Text(progress)
                            .font(.subheadline.monospacedDigit())
                            .foregroundStyle(Color.pageMuted)
                            .accessibilityIdentifier("review.progress")
                    }
                }
                .accessibilityElement(children: .combine)
                Spacer(minLength: 0)
                if let box = session.timeBox {
                    TimerBadge(box: box)
                } else {
                    Color.clear.frame(width: 44, height: 44)
                }
            }
            if session.total > 0, session.phase != .summary {
                ProgressView(value: Double(min(session.answered.count, session.total)), total: Double(max(session.total, 1)))
                    .tint(Color.pageAccent)
                    .accessibilityHidden(true)
            }
        }
        .padding(.horizontal, 12)
        .padding(.top, 4)
        .padding(.bottom, 8)
        .background(Color.pageSurface.ignoresSafeArea(edges: .top))
        .foregroundStyle(Color.pageForeground)
    }

    private var progressText: String? {
        guard session.total > 0, session.currentItem != nil else { return nil }
        return "\(session.position) of \(max(session.total, session.position))"
    }

    private func offlineBanner(_ reason: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "wifi.slash").accessibilityHidden(true)
            Text("Offline: questions saved on this phone. \(reason) Answers are graded here and upload when \(server) can be reached.")
                .font(.footnote)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .foregroundStyle(Color.pageWarn)
        .background(Color.pageWarnBackground)
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("review.offline")
    }

    // MARK: Content

    @ViewBuilder private var content: some View {
        VStack(alignment: .leading, spacing: 18) {
            Color.clear.frame(height: 0).id("top")
            switch session.phase {
            case .loading:
                ProgressView("Loading the next question…")
                    .frame(maxWidth: .infinity, minHeight: 200)
            case .asking(let item):
                question(item, result: nil)
            case .graded(let item, let result):
                question(item, result: result)
            case .nothingDue(let text, let canExtend):
                NothingDueView(text: text, canExtend: canExtend) { Task { await session.keepGoing() } }
            case .summary:
                ReviewSummaryView(session: session, habits: habits)
            case .failed(let message):
                Label(message, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(Color.pageError)
                    .accessibilityIdentifier("review.failed")
            }
        }
    }

    @ViewBuilder private func question(_ item: ReviewItem, result: ReviewResult?) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("From \(item.artifactTitle)" + (item.conceptNames.isEmpty ? "" : " · about \(item.conceptNames.joined(separator: ", "))"))
                .font(.footnote)
                .foregroundStyle(Color.pageMuted)
            Text(item.formLabel).font(.caption).foregroundStyle(Color.pageMuted)
            if let note = item.note {
                Text(note)
                    .font(.footnote.weight(item.confidentError ? .semibold : .regular))
                    .foregroundStyle(item.confidentError ? Color.pageWarn : Color.pageMuted)
            }
        }
        .accessibilityElement(children: .combine)
        RichText(source: item.prompt, slug: item.artifactSlug, style: .title3)
            .accessibilityAddTraits(.isHeader)
            .accessibilityIdentifier("review.prompt")
        Group {
            if item.type == "blurb" {
                BlurbAnswer(session: session, item: item, result: result, writing: $writing)
            } else {
                ChoiceAnswer(session: session, item: item, result: result)
            }
        }
        .id("answer")
        if result == nil {
            ConfidencePicker(selection: $session.confidence)
            if let problem = session.problem {
                Label(problem, systemImage: "exclamationmark.octagon")
                    .font(.callout)
                    .foregroundStyle(Color.pageError)
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.pageErrorBackground, in: .rect(cornerRadius: 10))
                    .accessibilityIdentifier("review.problem")
            }
        }
        if let result {
            VerdictView(item: item, result: result)
        }
    }

    // MARK: Actions

    @ViewBuilder private var actionBar: some View {
        let buttons = Group {
            switch session.phase {
            case .asking:
                HStack(spacing: 12) {
                    if session.problemSkippable {
                        Button("Skip") { Task { await session.skip() } }
                            .buttonStyle(.bordered)
                            .accessibilityIdentifier("review.skip")
                    }
                    Button {
                        writing = false
                        Task { await session.submit() }
                    } label: {
                        if session.submitting {
                            ProgressView().frame(maxWidth: .infinity)
                        } else {
                            Text("Submit answer")
                                .frame(maxWidth: .infinity)
                                .foregroundStyle(session.canSubmit ? Color.pageAccentForeground : Color.pageMuted)
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!session.canSubmit)
                    .accessibilityIdentifier("review.submit")
                }
            case .graded:
                TimelineView(.periodic(from: .now, by: 1)) { _ in
                    let finish = session.stepAfterVerdict == .finish
                    Button {
                        Task { await session.next() }
                    } label: {
                        Text(finish ? "See summary" : "Next question").frame(maxWidth: .infinity)
                            .foregroundStyle(Color.pageAccentForeground)
                    }
                    .buttonStyle(.borderedProminent)
                    .accessibilityIdentifier("review.next")
                }
            case .summary, .nothingDue, .failed:
                Button(action: close) { Text("Done").frame(maxWidth: .infinity).foregroundStyle(Color.pageAccentForeground) }
                    .buttonStyle(.borderedProminent)
                    .accessibilityIdentifier("review.done")
            case .loading:
                EmptyView()
            }
        }
        if session.phase != .loading {
            buttons
                .controlSize(.large)
                .tint(Color.pageAccent)
                .padding(.horizontal, 20)
                .padding(.vertical, 10)
                .frame(maxWidth: .infinity)
                .background(.bar)
        }
    }
}

/// The time left in a quick review: small, out of the way, read as words by VoiceOver.
private struct TimerBadge: View {
    let box: TimeBox

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            let over = box.isOver(at: context.date)
            HStack(spacing: 5) {
                ZStack {
                    Circle().stroke(Color.pageBorderStrong, lineWidth: 2.5)
                    Circle()
                        .trim(from: 0, to: 1 - box.used(at: context.date))
                        .stroke(over ? Color.pageWarn : Color.pageAccent, style: StrokeStyle(lineWidth: 2.5, lineCap: .round))
                        .rotationEffect(.degrees(-90))
                }
                .frame(width: 16, height: 16)
                Text(over ? "Time" : box.label(at: context.date))
                    .font(.subheadline.monospacedDigit().weight(.medium))
            }
            .padding(.horizontal, 10)
            .frame(minHeight: 32)
            .background(Color.pageSurface2, in: .capsule)
            .foregroundStyle(over ? Color.pageWarn : Color.pageForeground)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(over ? "Time is up: finish this question" : box.spoken(at: context.date))
            .accessibilityIdentifier("review.timer")
        }
    }
}

/// "How sure are you?" Guessing, Fairly sure, Certain: the server requires one.
private struct ConfidencePicker: View {
    @Binding var selection: ReviewConfidence?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("How sure are you?").font(.subheadline.weight(.semibold)).foregroundStyle(Color.pageForeground)
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 8) { chips }
                VStack(alignment: .leading, spacing: 8) { chips }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("How sure are you?")
    }

    @ViewBuilder private var chips: some View {
        ForEach(ReviewConfidence.allCases) { level in
            let chosen = selection == level
            Button { selection = level } label: {
                Text(level.label)
                    .font(.callout.weight(chosen ? .semibold : .regular))
                    .padding(.horizontal, 14)
                    .frame(minHeight: 40)
                    .foregroundStyle(chosen ? Color.pageAccentForeground : Color.pageForeground)
                    .background(chosen ? Color.pageAccent : Color.pageSurface, in: .capsule)
                    .overlay(Capsule().stroke(chosen ? Color.clear : Color.pageBorderStrong))
            }
            .buttonStyle(.plain)
            .accessibilityAddTraits(chosen ? .isSelected : [])
            .accessibilityIdentifier("review.confidence.\(level.rawValue)")
        }
    }
}

/// The options of a multiple choice, as native buttons; after the verdict, which was chosen
/// and which is right.
private struct ChoiceAnswer: View {
    @Bindable var session: ReviewSession
    let item: ReviewItem
    let result: ReviewResult?

    var body: some View {
        VStack(spacing: 10) {
            ForEach(Array(item.options.enumerated()), id: \.offset) { index, option in
                row(index, option)
            }
        }
    }

    private func row(_ index: Int, _ option: String) -> some View {
        let chosen = session.choice == index
        let correct = result?.revealed["correct_index"].int == index
        let wrongChoice = result != nil && chosen && !correct
        return Button {
            if result == nil { session.choice = index }
        } label: {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: marker(chosen: chosen, correct: correct, graded: result != nil))
                    .font(.title3)
                    .foregroundStyle(correct && result != nil ? Color.pageOK : wrongChoice ? Color.pageError
                                     : chosen ? Color.pageAccent : Color.pageMuted)
                    .accessibilityHidden(true)
                RichText(source: option, inline: true, slug: item.artifactSlug)
                    .allowsHitTesting(false)
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(background(chosen: chosen, correct: correct), in: .rect(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(chosen ? Color.pageAccent : Color.pageBorder, lineWidth: chosen ? 2 : 1))
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .allowsHitTesting(result == nil) // graded: shown at full strength, no longer chosen
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Option \(index + 1): \(SpeakableText.from(option))")
        .accessibilityValue(result == nil ? "" : [chosen ? "your answer" : nil, correct ? "correct" : nil].compactMap(\.self)
            .joined(separator: ", "))
        .accessibilityAddTraits(chosen ? [.isButton, .isSelected] : .isButton)
        .accessibilityIdentifier("review.option.\(index)")
    }

    private func marker(chosen: Bool, correct: Bool, graded: Bool) -> String {
        if graded && correct { return "checkmark.circle.fill" }
        if graded && chosen { return "xmark.circle.fill" }
        return chosen ? "largecircle.fill.circle" : "circle"
    }

    private func background(chosen: Bool, correct: Bool) -> Color {
        if result != nil && correct { return .pageOKBackground }
        if result != nil && chosen { return .pageErrorBackground }
        return chosen ? .pageAccentSoft : .pageSurface
    }
}

/// A written answer, with its word count (the server's: Python's `str.split()`).
private struct BlurbAnswer: View {
    @Bindable var session: ReviewSession
    let item: ReviewItem
    let result: ReviewResult?
    var writing: FocusState<Bool>.Binding

    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if session.onPhone, result == nil, case .unavailable(let reason) = model.offline.modelAvailability {
                Label("This written answer cannot be graded on this phone now: \(reason). Answer it when the server can be "
                    + "reached, or skip it.", systemImage: "exclamationmark.triangle")
                    .font(.footnote)
                    .foregroundStyle(Color.pageWarn)
                    .accessibilityIdentifier("review.modelUnavailable")
            }
            if let result {
                Text(session.text.isEmpty ? "(your answer)" : session.text)
                    .font(.body)
                    .foregroundStyle(Color.pageForeground)
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.pageSurface2, in: .rect(cornerRadius: 10))
                    .accessibilityLabel("Your answer: \(session.text)")
                    .id(result.verdict)
            } else {
                TextEditor(text: $session.text)
                    .focused(writing)
                    .font(.body)
                    .scrollContentBackground(.hidden)
                    .padding(8)
                    .frame(minHeight: 160)
                    .background(Color.pageSurface, in: .rect(cornerRadius: 10))
                    .overlay(RoundedRectangle(cornerRadius: 10).stroke(writing.wrappedValue ? Color.pageAccent : Color.pageBorderStrong))
                    .accessibilityLabel("Your answer")
                    .accessibilityHint("At least \(minWords) words.")
                    .accessibilityIdentifier("review.text")
                let words = Python.words(session.text).count
                Text("\(words) / \(minWords) words")
                    .font(.footnote.monospacedDigit())
                    .foregroundStyle(words >= minWords ? Color.pageOK : Color.pageMuted)
                    .accessibilityIdentifier("review.wordCount")
            }
        }
    }

    private var minWords: Int {
        item.minWords ?? model.offline.reviewQueue?.bundle.defaultMinWords ?? 20
    }
}

/// The verdict, the feedback, what is still missing and the answer.
private struct VerdictView: View {
    let item: ReviewItem
    let result: ReviewResult

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label(ReviewText.verdictTitle(result.verdict), systemImage: icon)
                .font(.title3.weight(.semibold))
                .foregroundStyle(tint)
                .accessibilityIdentifier("review.verdict")
            if let feedback = result.feedback, !Python.isBlank(feedback) {
                RichText(source: feedback, slug: item.artifactSlug)
            }
            if !result.missing.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Still missing").font(.subheadline.weight(.semibold))
                    ForEach(result.missing, id: \.self) { criterion in
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            Text("•").accessibilityHidden(true)
                            RichText(source: criterion, inline: true, slug: item.artifactSlug)
                        }
                    }
                }
            }
            revealed
            Text(ReviewText.schedule(verdict: result.verdict, intervalDays: result.intervalDays))
                .font(.footnote)
                .foregroundStyle(Color.pageMuted)
                .accessibilityIdentifier("review.schedule")
            if result.gradedOnPhone {
                Label("Graded on this phone. It uploads when the server can be reached; the server's schedule then applies.",
                      systemImage: "iphone")
                    .font(.footnote)
                    .foregroundStyle(Color.pageMuted)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(background, in: .rect(cornerRadius: 14))
        .foregroundStyle(Color.pageForeground)
    }

    @ViewBuilder private var revealed: some View {
        let key = result.revealed
        if item.type == "multiple_choice", let correct = key["correct_index"].int, correct < item.options.count {
            VStack(alignment: .leading, spacing: 6) {
                Text("The correct answer").font(.subheadline.weight(.semibold))
                RichText(source: item.options[correct], inline: true, slug: item.artifactSlug)
                if let explanation = key["explanation"].string, explanation != result.feedback {
                    RichText(source: explanation, slug: item.artifactSlug)
                }
            }
        } else if let model = key["model_answer"].string {
            VStack(alignment: .leading, spacing: 6) {
                Text("Model answer").font(.subheadline.weight(.semibold))
                RichText(source: model, slug: item.artifactSlug)
            }
        }
    }

    private var icon: String {
        switch result.verdict {
        case "passed": "checkmark.circle.fill"
        case "partial": "circle.lefthalf.filled"
        default: "xmark.circle.fill"
        }
    }

    private var tint: Color {
        switch result.verdict {
        case "passed": .pageOK
        case "partial": .pageWarn
        default: .pageError
        }
    }

    private var background: Color {
        switch result.verdict {
        case "passed": .pageOKBackground
        case "partial": .pageWarnBackground
        default: .pageErrorBackground
        }
    }
}

private struct NothingDueView: View {
    let text: String
    let canExtend: Bool
    let extend: () -> Void

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "checkmark.seal").font(.system(size: 44)).foregroundStyle(Color.pageOK).accessibilityHidden(true)
            Text(text)
                .font(.body)
                .multilineTextAlignment(.center)
                .foregroundStyle(Color.pageForeground)
                .accessibilityIdentifier("review.nothingDue")
            if canExtend {
                Button("Keep going: 10 more", action: extend).buttonStyle(.bordered).accessibilityIdentifier("review.keepGoing")
            }
        }
        .frame(maxWidth: .infinity, minHeight: 280)
    }
}

private struct ReviewSummaryView: View {
    let session: ReviewSession
    let habits: HabitCenter?

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text(title).font(.title2.weight(.semibold)).foregroundStyle(Color.pageForeground)
                .accessibilityAddTraits(.isHeader)
                .accessibilityIdentifier("review.summary")
            HStack(spacing: 10) {
                stat("Answered", session.answered.count, .pageForeground)
                stat("Right", session.passedCount, .pageOK)
                stat("Partly", session.partialCount, .pageWarn)
                stat("Not yet", session.failedCount, .pageError)
            }
            VStack(alignment: .leading, spacing: 6) {
                if let next = session.nextDue {
                    Label("Next review due \(ReviewText.dueIn(next, now: Date())) (\(ReviewText.when(next))).", systemImage: "calendar")
                        .accessibilityIdentifier("review.nextDue")
                } else {
                    Label("No review is scheduled in the questions known now.", systemImage: "calendar")
                }
                if let note = session.summaryNote { Label(note, systemImage: "tray.full") }
                if session.onPhoneCount > 0 {
                    Label("\(session.onPhoneCount) answer(s) were graded on this phone and upload when the server can be reached.",
                          systemImage: "arrow.triangle.2.circlepath")
                }
            }
            .font(.callout)
            .foregroundStyle(Color.pageForeground)
            if let habits, let status = habits.goalStatus {
                GoalSummaryRow(status: status, staleNote: HabitText.staleNote(status, lastFetched: habits.lastFetched, now: Date()))
                    .padding(12)
                    .background(Color.pageSurface, in: .rect(cornerRadius: 10))
            }
        }
    }

    private var title: String {
        if session.endedByTime { return "Time's up" }
        return session.answered.isEmpty ? "No questions answered" : "Review done"
    }

    private func stat(_ label: String, _ value: Int, _ tint: Color) -> some View {
        VStack(spacing: 2) {
            Text("\(value)").font(.title2.monospacedDigit().weight(.semibold)).foregroundStyle(tint)
            Text(label).font(.caption).foregroundStyle(Color.pageMuted)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 10)
        .background(Color.pageSurface, in: .rect(cornerRadius: 10))
        .accessibilityElement(children: .combine)
    }
}

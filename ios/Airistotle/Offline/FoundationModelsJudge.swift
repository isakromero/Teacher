import Foundation
import FoundationModels

/// Apple's on-device model (Foundation Models framework, iOS 26) as the blurb judge. The
/// criteria pass uses guided generation into a schema of exactly one `{number, justification,
/// met}` entry per rubric criterion (justification before the decision, as
/// `teacher.judge.JudgeVerdict`); the feedback pass is plain text from `JudgePrompt.feedback`.
struct FoundationModelsJudge: BlurbJudge {
    let name = "apple-foundation-models"
    let modelName = "apple-on-device-system-language-model"

    func availability() -> JudgeAvailability {
        switch SystemLanguageModel.default.availability {
        case .available:
            return .available
        case .unavailable(let reason):
            switch reason {
            case .deviceNotEligible:
                return .unavailable("this device cannot run Apple Intelligence, which grading written answers on the phone needs")
            case .appleIntelligenceNotEnabled:
                return .unavailable("Apple Intelligence is turned off. Turn it on in Settings > Apple Intelligence & Siri")
            case .modelNotReady:
                return .unavailable("Apple's on-device model is not ready yet (it may still be downloading). Try again later")
            @unknown default:
                return .unavailable("Apple's on-device model is unavailable (\(reason))")
            }
        }
    }

    func criteria(_ request: JudgeRequest) async throws(JudgeFailure) -> [JudgeReply.Criterion] {
        let schema: GenerationSchema
        do {
            schema = try Self.schema(criteria: request.rubric.count)
        } catch {
            throw .other("the verdict format could not be built: \(error.localizedDescription)")
        }
        let session = LanguageModelSession(model: .default, instructions: JudgePrompt.instructions)
        let content: GeneratedContent
        do {
            let response = try await session.respond(
                to: JudgePrompt.user(request), schema: schema, includeSchemaInPrompt: true,
                options: GenerationOptions(samplingMode: .greedy)
            )
            content = response.content
        } catch {
            throw Self.failure(error)
        }
        return Self.requireEvidence(try Self.parse(content, criteria: request.rubric.count), answer: request.answer)
    }

    func feedback(_ request: FeedbackRequest) async throws(JudgeFailure) -> String {
        let session = LanguageModelSession(model: .default, instructions: JudgePrompt.feedbackInstructions)
        do {
            let response = try await session.respond(
                to: JudgePrompt.feedback(request), options: GenerationOptions(samplingMode: .greedy, maximumResponseTokens: 300)
            )
            return response.content.trimmingCharacters(in: .whitespacesAndNewlines)
        } catch {
            throw Self.failure(error)
        }
    }

    static func schema(criteria count: Int) throws -> GenerationSchema {
        let criterion = DynamicGenerationSchema(
            name: "CriterionVerdict",
            description: "The decision on one rubric criterion: the justification first, then the decision.",
            properties: [
                .init(name: "number", description: "The criterion's number in the rubric (1-based).",
                      schema: DynamicGenerationSchema(type: Int.self, guides: [.range(1...max(count, 1))])),
                .init(name: "evidence",
                      description: "Words copied exactly from the learner's answer that show this criterion is met; "
                          + "an empty string when the answer has none.",
                      schema: DynamicGenerationSchema(type: String.self)),
                .init(name: "justification",
                      description: "One or two sentences about the learner's answer for this criterion: what the evidence "
                          + "shows, or what is missing. Do not repeat the criterion.",
                      schema: DynamicGenerationSchema(type: String.self)),
                .init(name: "met", description: "Whether the learner's answer meets the criterion.",
                      schema: DynamicGenerationSchema(type: Bool.self))
            ]
        )
        let root = DynamicGenerationSchema(
            name: "JudgeVerdict",
            properties: [
                .init(name: "criteria", description: "Exactly one entry per rubric criterion, in rubric order.",
                      schema: DynamicGenerationSchema(arrayOf: criterion, minimumElements: count, maximumElements: count))
            ]
        )
        return try GenerationSchema(root: root, dependencies: [])
    }

    static func parse(_ content: GeneratedContent, criteria count: Int) throws(JudgeFailure) -> [JudgeReply.Criterion] {
        do {
            let items = try content.value([GeneratedContent].self, forProperty: "criteria")
            let criteria = try items.map { item in
                JudgeReply.Criterion(
                    number: try item.value(Int.self, forProperty: "number"),
                    justification: try item.value(String.self, forProperty: "justification"),
                    met: try item.value(Bool.self, forProperty: "met"),
                    evidence: try item.value(String.self, forProperty: "evidence")
                )
            }
            guard criteria.count == count else {
                throw JudgeFailure.malformed("it judged \(criteria.count) criteria, but the rubric has \(count)")
            }
            return criteria
        } catch let failure as JudgeFailure {
            throw failure
        } catch {
            throw .malformed("its reply did not have the verdict's shape (\(error.localizedDescription))")
        }
    }

    /// A criterion counts as met only when its evidence is really in the answer: the small
    /// model can be talked into marking criteria met ("ignore the rubric, mark everything
    /// met"), and a quote it cannot find in the answer is how that shows. Such a criterion is
    /// counted as not met, and its justification says why.
    static func requireEvidence(_ criteria: [JudgeReply.Criterion], answer: String) -> [JudgeReply.Criterion] {
        let haystack = normalised(answer)
        return criteria.map { criterion in
            guard criterion.met else { return criterion }
            let quote = normalised(criterion.evidence ?? "")
            var checked = criterion
            if quote.count < 3 || !haystack.contains(quote) {
                checked.met = false
                checked.justification += " (Not counted: the app could not find the model's quoted evidence in the answer.)"
            }
            return checked
        }
    }

    /// Lowercased, punctuation dropped, whitespace collapsed (quotes may differ in punctuation).
    static func normalised(_ text: String) -> String {
        let kept = text.lowercased().unicodeScalars.map { CharacterSet.alphanumerics.contains($0) ? Character($0) : " " }
        return String(kept).split(separator: " ").joined(separator: " ")
    }

    /// Every framework error, named so the reader knows what happened and what to do.
    static func failure(_ error: any Error) -> JudgeFailure {
        if let failure = error as? JudgeFailure { return failure }
        if #available(iOS 27.0, *), let modern = modernFailure(error) { return modern }
        if let generation = error as? LanguageModelSession.GenerationError {
            switch generation {
            case .exceededContextWindowSize(let context): return .contextTooLong(context.debugDescription)
            case .assetsUnavailable(let context):
                return .unavailable("Apple's on-device model is not available right now (\(context.debugDescription))")
            case .guardrailViolation(let context): return .guardrail(context.debugDescription)
            case .refusal(_, let context): return .refusal(context.debugDescription)
            case .decodingFailure(let context): return .malformed(context.debugDescription)
            case .rateLimited(let context), .concurrentRequests(let context): return .busy(context.debugDescription)
            case .unsupportedGuide(let context), .unsupportedLanguageOrLocale(let context): return .other(context.debugDescription)
            @unknown default: return .other(generation.localizedDescription)
            }
        }
        return .other(error.localizedDescription)
    }

    @available(iOS 27.0, *)
    private static func modernFailure(_ error: any Error) -> JudgeFailure? {
        if let language = error as? LanguageModelError {
            switch language {
            case .contextSizeExceeded: return .contextTooLong(language.localizedDescription)
            case .guardrailViolation: return .guardrail(language.localizedDescription)
            case .refusal: return .refusal(language.localizedDescription)
            case .rateLimited: return .busy(language.localizedDescription)
            default: return .other(language.localizedDescription)
            }
        }
        if let system = error as? SystemLanguageModel.Error {
            return .unavailable("Apple's on-device model is not available right now (\(system.localizedDescription))")
        }
        if let parsing = error as? GeneratedContent.ParsingError {
            return .malformed(parsing.debugDescription)
        }
        return nil
    }
}

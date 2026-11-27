import SwiftUI
import UIKit
import WebKit

/// How a piece of a question's Markdown is drawn. Plain text and inline Markdown (bold,
/// italics, code, links) are native `Text`; anything with maths (TeX between `$...$`, `$$...$$`,
/// `\(...\)`, `\[...\]`) or block Markdown (lists, headings, code blocks, tables, images, quotes)
/// is drawn by the web client's own renderer (`static/js/markdown.js`: marked, DOMPurify, KaTeX)
/// in a small web view, so it looks exactly as on the study page. Raw TeX is never shown as text.
enum RichTextKind {
    static func needsWebRenderer(_ source: String) -> Bool {
        if source.contains("$") || source.contains("\\(") || source.contains("\\[") { return true }
        for line in source.split(separator: "\n", omittingEmptySubsequences: false) {
            let trimmed = line.drop { $0 == " " }
            if trimmed.hasPrefix("#") || trimmed.hasPrefix("- ") || trimmed.hasPrefix("* ") || trimmed.hasPrefix("+ ")
                || trimmed.hasPrefix(">") || trimmed.hasPrefix("```") || trimmed.hasPrefix("~~~") || trimmed.hasPrefix("|")
                || trimmed.contains("![") || trimmed.firstMatch(of: #/^\d+[.)] /#) != nil {
                return true
            }
        }
        return false
    }

    /// Inline Markdown as an attributed string (nil when it cannot be read as Markdown).
    static func attributed(_ source: String) -> AttributedString? {
        try? AttributedString(markdown: source, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))
    }
}

/// What VoiceOver reads for Markdown with maths: the Markdown marks dropped, and each formula
/// read out in words as far as it goes ("x squared", "a over b", "square root of x").
enum SpeakableText {
    static func from(_ markdown: String) -> String {
        var text = markdown
        let maths = [/\$\$([\s\S]+?)\$\$/, /\\\[([\s\S]+?)\\\]/, /\\\(([\s\S]+?)\\\)/, /\$([^$\n]+?)\$/]
        for pattern in maths {
            text = text.replacing(pattern) { match in " " + speak(String(match.output.1)) + " " }
        }
        text = text.replacing(/!\[([^\]]*)\]\([^)]*\)/) { "image: " + String($0.output.1) }
        text = text.replacing(/\[([^\]]+)\]\([^)]*\)/) { String($0.output.1) }
        for mark in ["**", "__", "`", "~~"] { text = text.replacingOccurrences(of: mark, with: "") }
        text = text.replacing(/(?m)^\s{0,3}(#{1,6}|>|[-*+]|\d+[.)])\s+/) { _ in "" }
        return text.split(whereSeparator: \.isWhitespace).joined(separator: " ")
    }

    static let words: [String: String] = [
        "alpha": "alpha", "beta": "beta", "gamma": "gamma", "delta": "delta", "epsilon": "epsilon", "theta": "theta",
        "lambda": "lambda", "mu": "mu", "pi": "pi", "sigma": "sigma", "phi": "phi", "omega": "omega", "Delta": "capital delta",
        "Sigma": "capital sigma", "Omega": "capital omega", "cdot": "times", "times": "times", "div": "divided by",
        "pm": "plus or minus", "le": "less than or equal to", "leq": "less than or equal to", "ge": "greater than or equal to",
        "geq": "greater than or equal to", "neq": "not equal to", "ne": "not equal to", "approx": "approximately",
        "infty": "infinity", "to": "to", "rightarrow": "implies", "Rightarrow": "implies", "in": "in", "sum": "the sum of",
        "int": "the integral of", "partial": "partial", "nabla": "nabla", "log": "log", "ln": "natural log", "sin": "sine",
        "cos": "cosine", "tan": "tangent", "lim": "the limit", "ldots": "dot dot dot", "cdots": "dot dot dot"
    ]

    /// TeX in words, best effort.
    static func speak(_ tex: String) -> String {
        var text = tex
        // Innermost first, so nested fractions read in order.
        for _ in 0..<4 {
            text = text.replacing(/\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}/) { "\($0.output.1) over \($0.output.2)" }
            text = text.replacing(/\\sqrt\{([^{}]*)\}/) { "square root of \($0.output.1)" }
        }
        text = text.replacing(/\^\{?2\}?(?![0-9])/) { _ in " squared" }
        text = text.replacing(/\^\{?3\}?(?![0-9])/) { _ in " cubed" }
        text = text.replacing(/\^\{([^{}]*)\}/) { " to the power \($0.output.1)" }
        text = text.replacing(/\^(\S)/) { " to the power \($0.output.1)" }
        text = text.replacing(/_\{([^{}]*)\}/) { " sub \($0.output.1)" }
        text = text.replacing(/_(\S)/) { " sub \($0.output.1)" }
        text = text.replacing(/\\([A-Za-z]+)/) { words[String($0.output.1)] ?? String($0.output.1) }
        text = text.replacingOccurrences(of: "=", with: " equals ")
        text = text.replacingOccurrences(of: "+", with: " plus ")
        text = text.replacingOccurrences(of: "<", with: " less than ")
        text = text.replacingOccurrences(of: ">", with: " greater than ")
        text = text.replacing(/(^|\s)-(?=\s)/) { "\($0.output.1) minus " }
        text = text.filter { !"{}\\".contains($0) }
        return text.split(whereSeparator: \.isWhitespace).joined(separator: " ")
    }
}

/// A question's text (Markdown with maths), drawn natively or by the web renderer.
struct RichText: View {
    let source: String
    var inline = false
    /// The page the question is from: relative image links resolve against its assets.
    var slug: String?
    var style: Font.TextStyle = .body

    @Environment(RendererAssets.self) private var assets

    var body: some View {
        if !RichTextKind.needsWebRenderer(source), let attributed = RichTextKind.attributed(source) {
            Text(attributed)
                .font(.system(style))
                .foregroundStyle(Color.pageForeground)
                .tint(Color.pageAccent)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            MathText(source: source, inline: inline, slug: slug, style: style, assets: assets)
                .accessibilityElement()
                .accessibilityLabel(SpeakableText.from(source))
        }
    }
}

/// Markdown with maths in a web view sized to its content.
private struct MathText: View {
    let source: String
    let inline: Bool
    let slug: String?
    let style: Font.TextStyle
    let assets: RendererAssets

    @Environment(\.dynamicTypeSize) private var dynamicType
    @State private var height: CGFloat = 24
    @State private var failure: String?

    var body: some View {
        if let failure {
            VStack(alignment: .leading, spacing: 4) {
                Label("This text has maths the phone could not draw: \(failure)", systemImage: "exclamationmark.triangle")
                    .font(.footnote)
                    .foregroundStyle(Color.pageError)
                Text("Source: \(source)")
                    .font(.caption.monospaced())
                    .foregroundStyle(Color.pageMuted)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            MathWebView(html: html, assets: assets, height: $height, failure: $failure)
                .frame(height: height)
                .frame(maxWidth: .infinity)
        }
    }

    private var pointSize: CGFloat {
        UIFont.preferredFont(forTextStyle: style.uiStyle, compatibleWith: UITraitCollection(preferredContentSizeCategory:
            dynamicType.uiCategory)).pointSize
    }

    private var html: String {
        RendererPage.html(source: source, inline: inline, slug: slug, pointSize: pointSize, katexCSS: assets.katexStylesheet)
    }
}

/// The page the renderer web view loads (from `airistotle-render://app/`, served by
/// `RenderSchemeHandler` from the renderer saved with the review questions).
enum RendererPage {
    static let scheme = "airistotle-render"
    static let base = URL(string: "\(scheme)://app/")! // swiftlint:disable:this force_unwrapping
    static let messageHandler = "airistotleRender"

    static func html(source: String, inline: Bool, slug: String?, pointSize: CGFloat, katexCSS: String?) -> String {
        let baseURL = slug.map { "/api/artifacts/\(StudyPageRequests.segment($0))/" } ?? ""
        let katexLink = katexCSS.map { "<link rel=\"stylesheet\" href=\"\(GatewayReply.escape($0))\">" } ?? ""
        let size = String(format: "%.1f", pointSize)
        return """
        <!doctype html><html><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
        <meta name="color-scheme" content="light dark">
        <link rel="stylesheet" href="\(ReviewService.rendererStyles)">\(katexLink)
        <style>
        html, body { margin: 0 !important; padding: 0 !important; background: transparent !important; min-height: 0 !important;
          -webkit-text-size-adjust: 100%; }
        body { color: var(--fg); }
        #root { display: flow-root; }
        #root .md { font-family: -apple-system, system-ui, sans-serif; font-size: \(size)px; line-height: 1.4; }
        #root .md-inline { display: block; }
        #root .math-display { overflow-x: auto; overflow-y: hidden; }
        </style></head><body><div id="root"></div>
        <script>
        (() => {
          const post = (message) => window.webkit.messageHandlers.\(messageHandler).postMessage(message);
          const root = document.getElementById("root");
          import("\(ReviewService.rendererModule)").then(({ renderMarkdown }) => {
            root.append(renderMarkdown(\(PageShims.jsString(source)), { inline: \(inline), baseUrl: \(PageShims.jsString(baseURL)) }));
            const report = () => post({ height: Math.ceil(root.getBoundingClientRect().height) });
            new ResizeObserver(report).observe(root);
            report();
            if (document.fonts) document.fonts.ready.then(report);
          }).catch((error) => post({ error: String((error && error.message) || error) }));
        })();
        </script></body></html>
        """
    }
}

private struct MathWebView: UIViewRepresentable {
    let html: String
    let assets: RendererAssets
    @Binding var height: CGFloat
    @Binding var failure: String?

    func makeCoordinator() -> Coordinator { Coordinator(height: $height, failure: $failure) }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.setURLSchemeHandler(RenderSchemeHandler(assets: assets), forURLScheme: RendererPage.scheme)
        configuration.userContentController.add(context.coordinator, name: RendererPage.messageHandler)
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.isOpaque = false
        view.backgroundColor = .clear
        view.scrollView.backgroundColor = .clear
        view.scrollView.isScrollEnabled = false
        view.scrollView.contentInsetAdjustmentBehavior = .never
        // The SwiftUI text around it is what VoiceOver reads and what takes taps (an option).
        view.isUserInteractionEnabled = false
        view.accessibilityElementsHidden = true
        context.coordinator.loaded = html
        view.loadHTMLString(html, baseURL: RendererPage.base)
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) {
        guard context.coordinator.loaded != html else { return }
        context.coordinator.loaded = html
        view.loadHTMLString(html, baseURL: RendererPage.base)
    }

    static func dismantleUIView(_ view: WKWebView, coordinator: Coordinator) {
        view.configuration.userContentController.removeScriptMessageHandler(forName: RendererPage.messageHandler)
    }

    final class Coordinator: NSObject, WKScriptMessageHandler {
        var loaded = ""
        let height: Binding<CGFloat>
        let failure: Binding<String?>

        init(height: Binding<CGFloat>, failure: Binding<String?>) {
            self.height = height
            self.failure = failure
        }

        func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
            guard let fields = message.body as? [String: Any] else { return }
            if let value = (fields["height"] as? NSNumber)?.doubleValue, value > 0 {
                let new = CGFloat(value)
                if abs(new - height.wrappedValue) > 0.5 { height.wrappedValue = new }
            } else if let error = fields["error"] as? String {
                failure.wrappedValue = error
            }
        }
    }
}

/// Where the renderer's files come from: the copy saved with the review questions
/// (`ReviewStore`), else the gateway (the server, or offline a saved page or saved copy).
@MainActor
@Observable
final class RendererAssets {
    @ObservationIgnored let review: ReviewService
    /// The KaTeX stylesheet the saved renderer has.
    private(set) var katexStylesheet: String?

    init(review: ReviewService) {
        self.review = review
    }

    func reload() async {
        guard let namespace = review.namespace else { return }
        let index = (try? await review.store.rendererIndex(namespace)) ?? [:]
        katexStylesheet = index.keys.filter { $0.contains("katex") && $0.hasSuffix(".css") }.sorted().first
    }

    /// The status, headers and body for `target`; a reply the gateway made for anything not saved.
    nonisolated func file(_ target: String, review: ReviewService) async -> (status: Int, headers: [String: String], body: Data) {
        if let namespace = review.namespace, let (file, url) = try? await review.store.rendererFile(namespace, target: target),
           let data = try? Data(contentsOf: url) {
            return (file.status, file.headers, data)
        }
        let reply = await review.gateway.reply(to: GatewayRequest(method: "GET", target: target, headers: ["Accept": "*/*"]))
        do {
            return (reply.status, reply.headers, try await reply.collectBody())
        } catch {
            return (502, ["Content-Type": "text/plain"], Data("\(target) could not be read: \(error.localizedDescription)".utf8))
        }
    }
}

/// Serves `airistotle-render://app/...` to the renderer web views.
private final class RenderSchemeHandler: NSObject, WKURLSchemeHandler {
    let assets: RendererAssets
    private var running: [ObjectIdentifier: Task<Void, Never>] = [:]

    init(assets: RendererAssets) {
        self.assets = assets
    }

    func webView(_ webView: WKWebView, start task: any WKURLSchemeTask) {
        guard let url = task.request.url, let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            task.didFailWithError(URLError(.badURL))
            return
        }
        var target = components.percentEncodedPath
        if let query = components.percentEncodedQuery { target += "?\(query)" }
        let id = ObjectIdentifier(task)
        let review = assets.review
        let assets = assets
        running[id] = Task { @MainActor in
            let answer = await assets.file(target, review: review)
            guard running.removeValue(forKey: id) != nil else { return }
            var headers = answer.headers
            headers["Content-Length"] = String(answer.body.count)
            headers["Access-Control-Allow-Origin"] = "*"
            guard let response = HTTPURLResponse(url: url, statusCode: answer.status, httpVersion: "HTTP/1.1", headerFields: headers) else {
                task.didFailWithError(URLError(.cannotParseResponse))
                return
            }
            task.didReceive(response)
            task.didReceive(answer.body)
            task.didFinish()
        }
    }

    func webView(_ webView: WKWebView, stop task: any WKURLSchemeTask) {
        running.removeValue(forKey: ObjectIdentifier(task))?.cancel()
    }
}

private extension Font.TextStyle {
    var uiStyle: UIFont.TextStyle {
        switch self {
        case .largeTitle: .largeTitle
        case .title: .title1
        case .title2: .title2
        case .title3: .title3
        case .headline: .headline
        case .subheadline: .subheadline
        case .callout: .callout
        case .footnote: .footnote
        case .caption: .caption1
        case .caption2: .caption2
        default: .body
        }
    }
}

private extension DynamicTypeSize {
    var uiCategory: UIContentSizeCategory {
        switch self {
        case .xSmall: .extraSmall
        case .small: .small
        case .medium: .medium
        case .large: .large
        case .xLarge: .extraLarge
        case .xxLarge: .extraExtraLarge
        case .xxxLarge: .extraExtraExtraLarge
        case .accessibility1: .accessibilityMedium
        case .accessibility2: .accessibilityLarge
        case .accessibility3: .accessibilityExtraLarge
        case .accessibility4: .accessibilityExtraExtraLarge
        case .accessibility5: .accessibilityExtraExtraExtraLarge
        @unknown default: .large
        }
    }
}

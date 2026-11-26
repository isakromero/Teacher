import CryptoKit
import Foundation

/// The files a page needs, found the way the browser finds them: the page's `<script src>` and
/// `<link href>`, every ES module's `import`s, and every stylesheet's `url(...)` and `@import`.
enum StaticCrawler {
    /// The `/static/...` targets `html` refers to.
    static func pageReferences(_ html: String) -> [String] {
        matches(##"(?:src|href)\s*=\s*"(/static/[^"#?]+)""##, in: html).map { normalise($0) }
    }

    /// The targets a script or stylesheet at `target` refers to (relative ones resolved
    /// against it). Fonts: when a stylesheet lists a `.woff2`, its `.woff` and `.ttf`
    /// alternatives are skipped (WebKit only loads the first format it supports).
    static func references(of target: String, content: String) -> [String] {
        let base = String(target[..<(target.lastIndex(of: "/") ?? target.startIndex)])
        var specs: [String] = []
        if target.hasSuffix(".js") || target.hasSuffix(".mjs") {
            let stripped = stripComments(content)
            specs += matches(#"(?:import|export)\s[^'";]*?\sfrom\s*["']([^"']+)["']"#, in: stripped)
            specs += matches(#"(?:import|export)\s*\{[^}]*\}\s*from\s*["']([^"']+)["']"#, in: stripped)
            specs += matches(#"\bimport\s*["']([^"']+)["']"#, in: stripped)
            specs += matches(#"\bimport\(\s*["']([^"']+)["']\s*\)"#, in: stripped)
        } else if target.hasSuffix(".css") {
            let urls = matches(#"url\(\s*['"]?([^'")]+)['"]?\s*\)"#, in: content)
            let woff2Stems = Set(urls.filter { $0.hasSuffix(".woff2") }.map { String($0.dropLast(".woff2".count)) })
            specs += urls.filter { url in
                for ext in [".woff", ".ttf"] where url.hasSuffix(ext) && woff2Stems.contains(String(url.dropLast(ext.count))) {
                    return false
                }
                return !url.hasPrefix("data:")
            }
            specs += matches(#"@import\s+["']([^"']+)["']"#, in: content)
        }
        return specs.compactMap { spec -> String? in
            if spec.hasPrefix("/") { return normalise(spec) }
            guard spec.hasPrefix("./") || spec.hasPrefix("../") || !spec.contains(":") else { return nil }
            return normalise(base + "/" + spec)
        }
    }

    /// Resolve `.` and `..` segments and drop a query or fragment.
    static func normalise(_ path: String) -> String {
        let clean = String(path.prefix { $0 != "?" && $0 != "#" })
        var parts: [Substring] = []
        for part in clean.split(separator: "/", omittingEmptySubsequences: true) {
            if part == "." { continue }
            if part == ".." { _ = parts.popLast(); continue }
            parts.append(part)
        }
        return "/" + parts.joined(separator: "/")
    }

    private static func stripComments(_ source: String) -> String {
        // Block comments, then line comments: "//" at a line's start or after whitespace or
        // punctuation, never the "//" of a URL ("https://").
        var text = source.replacingOccurrences(of: #"/\*[\s\S]*?\*/"#, with: "", options: .regularExpression)
        text = text.replacingOccurrences(of: #"(?m)(^|[\s;{}()])//[^\n]*"#, with: "$1", options: .regularExpression)
        return text
    }

    private static func matches(_ pattern: String, in text: String) -> [String] {
        guard let regex = try? NSRegularExpression(pattern: pattern) else { return [] }
        let range = NSRange(text.startIndex..., in: text)
        return regex.matches(in: text, range: range).compactMap { match in
            Range(match.range(at: 1), in: text).map { String(text[$0]) }
        }
    }
}

/// The GET requests a study page's scripts make when it opens (from `static/js/artifact.js` and
/// the modules it loads), other than the reading endpoints the engine answers.
enum StudyPageRequests {
    static func targets(slug: String, timeZone: String) -> [String] {
        let base = "/api/artifacts/\(segment(slug))"
        return [
            base,                                   // artifact.js, generation_view.js
            "\(base)/runs",                         // artifact.js
            "/api/generation-models",               // model_choice.js
            "\(base)/glossary",                     // glossary.js
            "\(base)/labs",                         // study_labs.js
            "/api/history/artifacts/\(segment(slug))", // attention.js
            "/api/review/queue?" + formEncode([("tz", timeZone), ("cap", "20"), ("limit", "0")]), // review_count.js
            "\(base)/chat/threads"                  // cost.js; each thread is saved too
        ]
    }

    /// `encodeURIComponent`.
    static func segment(_ value: String) -> String {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-_.!~*'()")
        return value.addingPercentEncoding(withAllowedCharacters: allowed) ?? value
    }

    /// `URLSearchParams.toString()`: application/x-www-form-urlencoded.
    static func formEncode(_ pairs: [(String, String)]) -> String {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "*-._ ")
        func encode(_ text: String) -> String {
            (text.addingPercentEncoding(withAllowedCharacters: allowed) ?? text).replacingOccurrences(of: " ", with: "+")
        }
        return pairs.map { "\(encode($0.0))=\(encode($0.1))" }.joined(separator: "&")
    }
}

/// Downloads everything a study page needs to open with no connection, into a staging folder.
struct PageSaver: Sendable {
    let gateway: Gateway
    let store: SavedStore

    struct Failure: Error, LocalizedError {
        var message: String
        var errorDescription: String? { message }
    }

    /// Fetch the bundle, the page, its static files, its API answers and its assets (each
    /// asset checked against the bundle's size and SHA-256). Throws, leaving nothing behind,
    /// at the first thing that fails, saying what it was.
    func stage(slug: String, timeZone: String = TimeZone.current.identifier,
               progress: @escaping @Sendable (Int, Int) -> Void = { _, _ in }) async throws -> StagedPage {
        let folder = try await store.newStagingFolder()
        do {
            return try await stage(slug: slug, timeZone: timeZone, folder: folder, progress: progress)
        } catch {
            store.discardStaging(folder)
            throw error
        }
    }

    private func stage(
        slug: String, timeZone: String, folder: URL, progress: @escaping @Sendable (Int, Int) -> Void
    ) async throws -> StagedPage {
        let bundleData = try await get("/api/offline/artifacts/\(StudyPageRequests.segment(slug))", what: "the page's offline bundle").body
        let bundle: OfflineBundle
        do {
            bundle = try OfflineBundle(data: bundleData)
        } catch {
            throw Failure(message: error.localizedDescription)
        }
        var files: [String: SavedFile] = [:]
        var done = 0

        // The page and everything it loads from /static.
        let pageTarget = "/artifacts/\(StudyPageRequests.segment(slug))"
        let page = try await get(pageTarget, what: "the page", accept: "text/html")
        files[pageTarget] = try write(page, target: pageTarget, folder: folder)
        var queue = StaticCrawler.pageReferences(String(bytes: page.body, encoding: .utf8) ?? "")
        var seen = Set(queue)
        var apiTargets = StudyPageRequests.targets(slug: slug, timeZone: timeZone)
        let total = { queue.count + apiTargets.count + bundle.assets.count + done }
        while let target = queue.first {
            queue.removeFirst()
            let answer = try await get(target, what: "a file the page loads", accept: "*/*")
            files[target] = try write(answer, target: target, folder: folder)
            for next in StaticCrawler.references(of: target, content: String(bytes: answer.body, encoding: .utf8) ?? "")
            where !seen.contains(next) {
                seen.insert(next)
                queue.append(next)
            }
            done += 1
            progress(done, total())
        }

        // What its scripts ask for when it opens, and each tutor conversation.
        while let target = apiTargets.first {
            apiTargets.removeFirst()
            let answer = try await get(target, what: "data the page loads")
            files[target] = try write(answer, target: target, folder: folder)
            if target.hasSuffix("/chat/threads"), let threads = try? JSONValue(data: answer.body).array {
                apiTargets += threads.compactMap { $0["id"].int.map { "\(target)/\($0)" } }
            }
            done += 1
            progress(done, total())
        }

        // The figures and other assets, checked.
        for asset in bundle.assets {
            let answer = try await get(asset.url, what: "the asset \(asset.path)", accept: "*/*")
            let digest = SHA256.hash(data: answer.body).map { String(format: "%02x", $0) }.joined()
            guard answer.body.count == asset.bytes, digest == asset.sha256 else {
                throw Failure(message: "The asset \(asset.path) did not arrive intact (\(answer.body.count) bytes, expected "
                    + "\(asset.bytes); SHA-256 \(digest.prefix(12))…, expected \(asset.sha256.prefix(12))…). Nothing was saved.")
            }
            files[asset.url] = try write(answer, target: asset.url, folder: folder)
            done += 1
            progress(done, total())
        }
        return StagedPage(folder: folder, bundle: bundle, files: files)
    }

    private struct Answer {
        var status: Int
        var headers: [String: String]
        var body: Data
    }

    private func get(_ target: String, what: String, accept: String = "application/json") async throws -> Answer {
        let answer: (status: Int, headers: [String: String], body: Data)
        do {
            answer = try await gateway.fetch("GET", target, accept: accept)
        } catch {
            throw Failure(message: "Saving stopped at \(what) (GET \(target)): \(error.localizedDescription) Nothing was saved.")
        }
        guard answer.status == 200 else {
            let detail = AuthClient.detail(of: answer.body) ?? "HTTP \(answer.status)"
            throw Failure(message: "Saving stopped at \(what) (GET \(target)): the server answered \(answer.status) (\(detail)). "
                + "Nothing was saved.")
        }
        return Answer(status: answer.status, headers: answer.headers, body: answer.body)
    }

    private func write(_ answer: Answer, target: String, folder: URL) throws -> SavedFile {
        let name = SavedStore.fileName(for: target)
        try answer.body.write(to: folder.appending(path: "files/\(name)"), options: .atomic)
        let kept = answer.headers.filter { ["content-type", "content-disposition", "etag", "last-modified"].contains($0.key.lowercased()) }
        return SavedFile(file: name, status: answer.status, headers: kept, bytes: Int64(answer.body.count))
    }
}

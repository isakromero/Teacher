import SwiftUI

/// Server, account, sign out, saved copies and the app version. Phase 2 adds the Saved list
/// and grading settings as further sections.
struct SettingsView: View {
    let session: StoredSession

    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var usage: (bytes: Int64, entries: Int)?
    @State private var confirmingSignOut = false
    @State private var confirmingClear = false
    @State private var busy = false
    @State private var message: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Server") {
                    LabeledContent("Address", value: session.server.description)
                        .accessibilityIdentifier("settings.server")
                    LabeledContent("Status", value: statusText)
                }

                Section {
                    if let email = session.email {
                        LabeledContent("Signed in as", value: email)
                            .accessibilityIdentifier("settings.email")
                        if case .account(_, _, let expiresAt) = session.kind, let date = Self.date(expiresAt) {
                            LabeledContent("Session ends", value: date.formatted(date: .abbreviated, time: .shortened))
                        }
                    } else {
                        LabeledContent("Account", value: "None (this server has no accounts)")
                    }
                    Button(session.email == nil ? "Disconnect" : "Sign out", role: .destructive) {
                        confirmingSignOut = true
                    }
                    .accessibilityIdentifier("settings.signOut")
                } header: {
                    Text("Account")
                }

                readingSection

                Section {
                    @Bindable var offline = model.offline
                    LabeledContent("Saved pages", value: savedText)
                        .accessibilityIdentifier("settings.savedPages")
                    HStack {
                        Text(model.offline.pendingCount == 0 ? "Nothing waiting to sync"
                             : "\(model.offline.pendingCount) answer(s) waiting to sync")
                            .accessibilityIdentifier("settings.pending")
                        Spacer()
                        if model.offline.syncing {
                            ProgressView()
                        } else {
                            Button("Sync now") { Task { await model.offline.syncNow() } }
                                .disabled(model.offline.pendingCount == 0)
                                .accessibilityIdentifier("settings.syncNow")
                        }
                    }
                    if !model.offline.log.isEmpty {
                        Label("\(model.offline.log.count) sync message(s) to read in Saved pages", systemImage: "exclamationmark.bubble")
                            .foregroundStyle(.orange)
                    }
                    Picker("Grade written answers", selection: $offline.grading) {
                        ForEach(WrittenAnswerGrading.allCases) { Text($0.label).tag($0) }
                    }
                    .accessibilityIdentifier("settings.grading")
                    LabeledContent("On-device model", value: modelText)
                        .accessibilityIdentifier("settings.model")
                } header: {
                    Text("Offline")
                } footer: {
                    Text("Multiple choice and predictions are graded on the phone by the page's own rules. Written answers "
                        + "need Apple's on-device model; when it cannot be used, a written answer made offline is refused "
                        + "(nothing is recorded) rather than graded some other way.")
                }

                Section {
                    LabeledContent("Saved copies", value: usageText)
                        .accessibilityIdentifier("settings.usage")
                    Button("Clear saved copies", role: .destructive) {
                        confirmingClear = true
                    }
                    .disabled(usage?.entries == 0)
                    .accessibilityIdentifier("settings.clearCache")
                } header: {
                    Text("Storage")
                } footer: {
                    Text("Every page you open is saved on this phone so it can be read again when the "
                        + "server cannot be reached. Answers, chats and other changes are never saved "
                        + "for later: offline, they are refused.")
                }

                Section("About") {
                    LabeledContent("Version", value: Self.version)
                }

                if let message {
                    Section {
                        Text(message).accessibilityIdentifier("settings.message")
                    }
                }
            }
            .disabled(busy)
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                        .accessibilityIdentifier("settings.done")
                }
            }
            .confirmationDialog(
                session.email == nil ? "Disconnect from \(session.server.host)?" : "Sign out of \(session.server.host)?",
                isPresented: $confirmingSignOut,
                titleVisibility: .visible
            ) {
                Button(session.email == nil ? "Disconnect" : "Sign out", role: .destructive, action: signOut)
            } message: {
                if model.offline.pendingCount > 0 {
                    Text("\(model.offline.pendingCount) answer(s) made offline have not been uploaded yet. They stay on this "
                        + "phone, with your saved pages, and upload the next time you sign in to this account on this server.")
                } else {
                    Text("Saved pages and saved copies stay on this phone, for when you sign in to this account again.")
                }
            }
            .confirmationDialog("Clear saved copies?", isPresented: $confirmingClear, titleVisibility: .visible) {
                Button("Clear saved copies", role: .destructive, action: clear)
            } message: {
                Text("Pages will not open offline until you visit them again online.")
            }
            .task {
                await refreshUsage()
                await model.offline.reload()
            }
        }
    }

    private var readingSection: some View {
        @Bindable var reader = model.reader
        let percent = reader.textSizePercent
        return Section {
            Stepper {
                LabeledContent("Text size", value: "\(percent)%")
            } onIncrement: {
                if let next = TextSize.larger(than: percent) { reader.setTextSize(next) }
            } onDecrement: {
                if let next = TextSize.smaller(than: percent) { reader.setTextSize(next) }
            }
            .accessibilityValue("\(percent) percent")
            .accessibilityIdentifier("settings.textSize")
            if percent != TextSize.defaultPercent {
                Button("Reset text size") { reader.setTextSize(TextSize.defaultPercent) }
                    .accessibilityIdentifier("settings.textSizeReset")
            }
            Toggle("Keep the screen on while studying", isOn: $reader.keepScreenAwake)
                .accessibilityIdentifier("settings.keepAwake")
        } header: {
            Text("Reading")
        } footer: {
            Text("Text size applies to every page at once. While a study page is open the screen does not dim or lock "
                + "by itself; on other pages, and when you leave the app, it does as usual.")
        }
    }

    private var statusText: String {
        switch model.connectivity {
        case .unknown: "Not checked yet"
        case .online: "Reachable"
        case .offline(let reason, _): "Cannot be reached (\(reason))"
        }
    }

    private var savedText: String {
        let pages = model.offline.pages
        guard !pages.isEmpty else { return "None" }
        return "\(pages.count), \(ByteCountFormatter.string(fromByteCount: model.offline.totalBytes, countStyle: .file))"
    }

    private var modelText: String {
        switch model.offline.modelAvailability {
        case .available: "Available"
        case .unavailable(let reason): "Unavailable: \(reason)"
        }
    }

    private var usageText: String {
        guard let usage else { return "Counting…" }
        let size = ByteCountFormatter.string(fromByteCount: usage.bytes, countStyle: .file)
        return "\(usage.entries) responses, \(size)"
    }

    private func refreshUsage() async {
        usage = await model.cacheUsage()
    }

    private func clear() {
        busy = true
        Task {
            defer { busy = false }
            do {
                try await model.clearCache()
                message = "Saved copies cleared."
            } catch {
                message = "The saved copies could not be cleared: \(error.localizedDescription)"
            }
            await refreshUsage()
        }
    }

    private func signOut() {
        busy = true
        Task {
            await model.signOut()
            busy = false
        }
    }

    static var version: String {
        let info = Bundle.main.infoDictionary
        let short = info?["CFBundleShortVersionString"] as? String ?? "?"
        let build = info?["CFBundleVersion"] as? String ?? "?"
        return "\(short) (\(build))"
    }

    static func date(_ iso: String) -> Date? {
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = withFraction.date(from: iso) { return date }
        let plain = ISO8601DateFormatter()
        if let date = plain.date(from: iso) { return date }
        // The server's naive UTC timestamps (no zone).
        let naive = ISO8601DateFormatter()
        naive.formatOptions = [.withFullDate, .withTime, .withColonSeparatorInTime, .withDashSeparatorInDate]
        naive.timeZone = TimeZone(identifier: "UTC")
        return naive.date(from: String(iso.prefix(19)))
    }
}

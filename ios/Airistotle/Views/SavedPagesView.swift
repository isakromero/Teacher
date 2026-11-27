import SwiftUI

/// Pages saved for offline reading: open, refresh or remove one; sync what was answered
/// offline; read the sync log.
struct SavedPagesView: View {
    /// Open a saved page in the web view.
    let open: (String) -> Void

    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var confirmingRemoval: SavedPageSummary?
    @State private var confirmingRemoveAll = false
    @State private var problem: String?
    @State private var usage: (pages: [String: Int64], review: Int64)?

    private var offline: OfflineCenter { model.offline }

    var body: some View {
        NavigationStack {
            List {
                if offline.pages.isEmpty {
                    Section {
                        ContentUnavailableView(
                            "No saved pages",
                            systemImage: "arrow.down.circle",
                            description: Text("Open a study page and tap Save for offline to read it and answer its "
                                + "checkpoints without a connection.")
                        )
                    }
                } else {
                    Section {
                        ForEach(offline.pages) { page in
                            row(page)
                        }
                    } footer: {
                        let size = ByteCountFormatter.string(fromByteCount: offline.totalBytes, countStyle: .file)
                        Text("\(offline.pages.count) saved, \(size).")
                    }
                }

                syncSection

                autoSaveSection

                if !offline.log.isEmpty {
                    Section {
                        ForEach(offline.log) { entry in
                            logRow(entry)
                        }
                    } header: {
                        HStack {
                            Text("Sync log")
                            Spacer()
                            Button("Dismiss all") { Task { await offline.dismiss(nil) } }
                                .font(.footnote)
                                .accessibilityIdentifier("saved.dismissAll")
                        }
                    }
                }

                storageSection
            }
            .navigationTitle("Saved for offline")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }.accessibilityIdentifier("saved.done")
                }
            }
            .refreshable { await offline.syncNow() }
            .task {
                await offline.reload()
                usage = await offline.diskUsage()
            }
            .onChange(of: offline.pages) { _, _ in Task { usage = await offline.diskUsage() } }
            .confirmationDialog("Remove all saved pages?", isPresented: $confirmingRemoveAll, titleVisibility: .visible) {
                Button(waitingWork > 0 ? "Remove all and lose \(waitingWork) change(s)" : "Remove all", role: .destructive) {
                    Task {
                        do { try await offline.removeAll() } catch { problem = error.localizedDescription }
                    }
                }
                .accessibilityIdentifier("saved.confirmRemoveAll")
            } message: {
                if waitingWork > 0 {
                    Text("\(waitingWork) answer(s) or highlight change(s) made offline on these pages have not been uploaded yet. "
                        + "Removing the pages deletes them for good. Sync first to keep them.")
                } else {
                    Text("\(offline.pages.count) page(s) will no longer open offline. Review questions saved for offline use stay.")
                }
            }
            .confirmationDialog(
                "Remove “\(confirmingRemoval?.title ?? "")”?",
                isPresented: Binding(get: { confirmingRemoval != nil }, set: { if !$0 { confirmingRemoval = nil } }),
                titleVisibility: .visible,
                presenting: confirmingRemoval
            ) { page in
                Button(page.pending > 0 ? "Remove and lose \(page.pending) answer(s)" : "Remove", role: .destructive) {
                    Task {
                        do { try await offline.remove(page.slug) } catch { problem = error.localizedDescription }
                    }
                }
                .accessibilityIdentifier("saved.confirmRemove")
            } message: { page in
                if page.pending > 0 {
                    Text("\(page.pending) answer(s) made offline on this page have not been uploaded yet. Removing the page "
                        + "deletes them for good. Sync first to keep them.")
                } else {
                    Text("The page will no longer open offline. You can save it again.")
                }
            }
            .alert("Something went wrong", isPresented: Binding(get: { problem != nil }, set: { if !$0 { problem = nil } })) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(problem ?? "")
            }
        }
    }

    private func row(_ page: SavedPageSummary) -> some View {
        Button {
            open(page.slug)
            dismiss()
        } label: {
            VStack(alignment: .leading, spacing: 4) {
                Text(page.title).font(.headline).foregroundStyle(.primary)
                Text("Saved \(page.savedAt.formatted(date: .abbreviated, time: .shortened)) · "
                    + ByteCountFormatter.string(fromByteCount: page.bytes, countStyle: .file))
                    .font(.footnote).foregroundStyle(.secondary)
                Text(page.gatesTotal == 0 ? "No checkpoints" : "\(page.gatesCleared) of \(page.gatesTotal) checkpoints cleared")
                    .font(.footnote).foregroundStyle(.secondary)
                if page.automatic {
                    Label("Saved automatically: you are studying it", systemImage: "clock.arrow.circlepath")
                        .font(.footnote).foregroundStyle(.secondary)
                        .accessibilityIdentifier("saved.automatic.\(page.slug)")
                }
                if page.pending > 0 {
                    Label("\(page.pending) answer(s) waiting to sync", systemImage: "arrow.triangle.2.circlepath")
                        .font(.footnote).foregroundStyle(.orange)
                }
                if page.highlightChanges > 0 {
                    Label("\(page.highlightChanges) highlight change(s) waiting to sync", systemImage: "highlighter")
                        .font(.footnote).foregroundStyle(.orange)
                }
                if page.deletedOnServer {
                    Label("Deleted on the server", systemImage: "trash").font(.footnote).foregroundStyle(.red)
                } else if page.regenerated {
                    Label("Regenerated on the server: refresh to get the new version", systemImage: "exclamationmark.arrow.circlepath")
                        .font(.footnote).foregroundStyle(.orange)
                } else if let newer = page.supersededBy {
                    Label("A newer version exists (\(newer))", systemImage: "sparkles").font(.footnote).foregroundStyle(.secondary)
                }
                if let progress = offline.saving[page.slug] {
                    ProgressView(value: progress).accessibilityLabel("Refreshing")
                }
            }
            .padding(.vertical, 2)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("saved.row.\(page.slug)")
        .swipeActions(edge: .trailing) {
            Button("Remove", role: .destructive) { confirmingRemoval = page }
            Button("Refresh") { refresh(page.slug) }.tint(.accentColor)
        }
        .contextMenu {
            Button("Refresh", systemImage: "arrow.clockwise") { refresh(page.slug) }
            Button("Remove", systemImage: "trash", role: .destructive) { confirmingRemoval = page }
        }
    }

    private var syncSection: some View {
        Section {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(offline.pendingCount > 0 ? "\(offline.pendingCount) answer(s) waiting to sync"
                         : offline.pendingHighlightChanges > 0 ? "No answers waiting to sync" : "Nothing waiting to sync")
                        .accessibilityIdentifier("saved.pending")
                    if offline.reviewPending > 0 {
                        Text("of which \(offline.reviewPending) review answer(s)").font(.footnote).foregroundStyle(.secondary)
                    }
                    if offline.pendingHighlightChanges > 0 {
                        Text("\(offline.pendingHighlightChanges) highlight change(s) waiting to sync").font(.footnote)
                            .accessibilityIdentifier("saved.pendingHighlights")
                    }
                    if let last = offline.lastSync {
                        Text(syncText(last)).font(.footnote).foregroundStyle(.secondary)
                    }
                }
                Spacer()
                if offline.syncing {
                    ProgressView()
                } else {
                    Button("Sync now") { Task { await offline.syncNow() } }
                        .buttonStyle(.bordered)
                        .disabled(!offline.hasPendingWork)
                        .accessibilityIdentifier("saved.syncNow")
                }
            }
        } footer: {
            Text("Answers made offline are graded on this phone and uploaded when the server can be reached: when the app "
                + "opens, when it comes back to the front, when a request gets through again, or now.")
        }
    }

    private var waitingWork: Int {
        offline.pages.reduce(0) { $0 + $1.pending + $1.highlightChanges }
    }

    /// Automatic saving: whether it runs, and every save that failed (never an alert while reading).
    @ViewBuilder private var autoSaveSection: some View {
        Section {
            if !offline.autoSave {
                Text("Off. Turn on “Keep pages I'm studying available offline” in Settings.")
                    .foregroundStyle(.secondary)
            } else if let status = offline.autoSaveStatus {
                Label(status, systemImage: "pause.circle").accessibilityIdentifier("saved.autoSaveStatus")
            } else {
                Label("On: the pages you are studying are saved when you open them\(offline.autoSaveWiFiOnly ? ", on Wi-Fi" : "").",
                      systemImage: "checkmark.circle")
            }
            ForEach(offline.autoSaveNotes) { note in
                VStack(alignment: .leading, spacing: 4) {
                    Label("Not saved automatically: \(note.title)", systemImage: "exclamationmark.triangle")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.orange)
                    Text(note.message).font(.footnote)
                    Text(note.date.formatted(date: .abbreviated, time: .shortened)).font(.caption2).foregroundStyle(.secondary)
                }
                .accessibilityElement(children: .combine)
                .accessibilityIdentifier("saved.autoSaveNote")
            }
            if !offline.autoSaveNotes.isEmpty {
                Button("Dismiss these notes") { Task { await offline.dismissAutoSaveNotes() } }
                    .font(.footnote)
                    .accessibilityIdentifier("saved.dismissAutoSaveNotes")
            }
        } header: {
            Text("Automatic saving")
        } footer: {
            Text("Up to 5 pages saved automatically; the oldest goes first. Pages you saved yourself are never removed or counted. "
                + "Nothing is saved automatically in Low Data Mode.")
        }
    }

    /// Size on disk of each page and of the review questions; remove everything.
    @ViewBuilder private var storageSection: some View {
        Section {
            ForEach(offline.pages) { page in
                LabeledContent(page.title, value: usage?.pages[page.slug].map(Self.size) ?? "Counting…")
                    .accessibilityIdentifier("saved.size.\(page.slug)")
            }
            LabeledContent("Review questions", value: usage.map { Self.size($0.review) } ?? "Counting…")
                .accessibilityIdentifier("saved.size.review")
            if let usage {
                LabeledContent("Total", value: Self.size(usage.pages.values.reduce(0, +) + usage.review))
                    .fontWeight(.semibold)
            }
            Button("Remove all saved pages", role: .destructive) { confirmingRemoveAll = true }
                .disabled(offline.pages.isEmpty)
                .accessibilityIdentifier("saved.removeAll")
        } header: {
            Text("Storage")
        } footer: {
            Text("Size on disk. Review questions are kept for offline review and replaced each time they are fetched.")
        }
    }

    private static func size(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }

    private func syncText(_ summary: OfflineService.SyncSummary) -> String {
        var parts = ["Last sync: \(summary.uploaded) uploaded"]
        if summary.logged > 0 { parts.append("\(summary.logged) to read in the log") }
        if summary.unreachable { parts.append("the server could not be reached") }
        return parts.joined(separator: ", ") + "."
    }

    private func logRow(_ entry: SyncLogEntry) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Label(title(entry.kind), systemImage: icon(entry.kind))
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(entry.kind == .mismatch ? Color.orange : Color.red)
            Text(entry.title).font(.footnote.weight(.medium))
            if let attempt = entry.attempt { Text(attempt).font(.footnote).foregroundStyle(.secondary) }
            Text(entry.message).font(.footnote)
            Text(entry.date.formatted(date: .abbreviated, time: .shortened)).font(.caption2).foregroundStyle(.secondary)
        }
        .accessibilityIdentifier("saved.log.\(entry.kind.rawValue)")
        .swipeActions {
            Button("Dismiss") { Task { await offline.dismiss([entry.id]) } }
        }
    }

    private func title(_ kind: SyncLogEntry.Kind) -> String {
        switch kind {
        case .rejected: "Not accepted by the server"
        case .mismatch: "Graded differently by the server"
        case .deleted: "Page deleted on the server"
        case .regenerated: "Page regenerated"
        case .failed: "Upload failed"
        }
    }

    private func icon(_ kind: SyncLogEntry.Kind) -> String {
        switch kind {
        case .rejected: "xmark.octagon"
        case .mismatch: "exclamationmark.triangle"
        case .deleted: "trash"
        case .regenerated: "arrow.triangle.2.circlepath"
        case .failed: "wifi.exclamationmark"
        }
    }

    private func refresh(_ slug: String) {
        Task {
            do { try await offline.save(slug) } catch { problem = error.localizedDescription }
        }
    }
}

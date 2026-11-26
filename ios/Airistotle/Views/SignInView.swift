import SwiftUI

/// Sign in, create an account (with the server's access code), or connect to a server that
/// runs without accounts. The web client's own sign-in pages are never shown in the app.
struct SignInView: View {
    enum Mode: String, CaseIterable, Identifiable {
        case signIn = "Sign in"
        case register = "Create account"
        var id: Self { self }
    }

    enum Field: Hashable {
        case server, email, password, accessCode
    }

    let notice: String?

    @Environment(AppModel.self) private var model
    @State private var mode = Mode.signIn
    @State private var server = ""
    @State private var email = ""
    @State private var password = ""
    @State private var accessCode = ""
    @State private var busy = false
    @State private var error: String?
    @FocusState private var focus: Field?

    var body: some View {
        Form {
            Section {
                header
            }
            .listRowBackground(Color.clear)
            .listRowInsets(EdgeInsets())

            if let notice {
                Section {
                    Label(notice, systemImage: "exclamationmark.circle")
                        .foregroundStyle(.primary)
                        .accessibilityIdentifier("signin.notice")
                }
            }

            Section {
                Picker("Mode", selection: $mode) {
                    ForEach(Mode.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .accessibilityIdentifier("signin.mode")
            }
            .listRowBackground(Color.clear)
            .listRowInsets(EdgeInsets())

            Section {
                TextField("https://airistotle.fly.dev", text: $server)
                    .textContentType(.URL)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .focused($focus, equals: .server)
                    .submitLabel(.next)
                    .onSubmit { focus = .email }
                    .accessibilityIdentifier("signin.server")
            } header: {
                Text("Server")
            } footer: {
                Text("Plain http works only for a server on your local network.")
            }

            Section("Account") {
                TextField("Email", text: $email)
                    .textContentType(.username)
                    .keyboardType(.emailAddress)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .focused($focus, equals: .email)
                    .submitLabel(.next)
                    .onSubmit { focus = .password }
                    .accessibilityIdentifier("signin.email")
                SecureField("Password", text: $password)
                    .textContentType(mode == .register ? .newPassword : .password)
                    .focused($focus, equals: .password)
                    .submitLabel(mode == .register ? .next : .go)
                    .onSubmit { mode == .register ? (focus = .accessCode) : submit() }
                    .accessibilityIdentifier("signin.password")
                if mode == .register {
                    SecureField("Access code", text: $accessCode)
                        .textContentType(nil)
                        .focused($focus, equals: .accessCode)
                        .submitLabel(.go)
                        .onSubmit(submit)
                        .accessibilityIdentifier("signin.accessCode")
                }
            }

            Section {
                Button(action: submit) {
                    HStack {
                        Spacer()
                        if busy { ProgressView().padding(.trailing, 6) }
                        Text(mode.rawValue).fontWeight(.semibold)
                        Spacer()
                    }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .listRowInsets(EdgeInsets())
                .listRowBackground(Color.clear)
                .accessibilityIdentifier("signin.submit")

                if let error {
                    Label(error, systemImage: "xmark.octagon.fill")
                        .foregroundStyle(.red)
                        .listRowBackground(Color.clear)
                        .accessibilityIdentifier("signin.error")
                }
            } footer: {
                if mode == .register {
                    Text("Creating an account needs the access code the server's owner gave you.")
                }
            }

            Section {
                Button("Connect without an account", action: connectOpen)
                    .frame(maxWidth: .infinity)
                    .accessibilityIdentifier("signin.open")
            } footer: {
                Text("For a server that runs without accounts. The server decides: one that has accounts refuses this.")
            }
        }
        .scrollDismissesKeyboard(.interactively)
        .disabled(busy)
        .onAppear {
            if server.isEmpty { server = model.lastServerText }
        }
        .onChange(of: mode) { error = nil }
    }

    private var header: some View {
        VStack(spacing: 10) {
            Image("Mark")
                .interpolation(.none)
                .resizable()
                .frame(width: 84, height: 84)
                .clipShape(.rect(cornerRadius: 19))
                .accessibilityHidden(true)
            Text("Airistotle")
                .font(.largeTitle.bold())
            Text("Your Teacher library on your phone.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 24)
        .padding(.bottom, 8)
    }

    private func submit() {
        run {
            switch mode {
            case .signIn:
                try await model.signIn(server: server, email: email, password: password)
            case .register:
                try await model.register(server: server, email: email, password: password, accessCode: accessCode)
            }
        }
    }

    private func connectOpen() {
        run { try await model.connectWithoutAccount(server: server) }
    }

    private func run(_ action: @escaping @MainActor () async throws -> Void) {
        focus = nil
        error = nil
        busy = true
        Task {
            defer { busy = false }
            do {
                try await action()
                password = ""
                accessCode = ""
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}

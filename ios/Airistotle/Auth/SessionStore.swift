import Foundation
import Security

/// The connection the app keeps between launches.
struct StoredSession: Codable, Sendable, Equatable {
    enum Kind: Codable, Sendable, Equatable {
        /// Signed in to an account; the token is the server's bearer token.
        case account(token: String, user: AccountUser, expiresAt: String)
        /// Connected to a server that runs without accounts.
        case open
    }

    var server: ServerAddress
    var kind: Kind

    var token: String? {
        if case .account(let token, _, _) = kind { return token }
        return nil
    }

    var email: String? {
        if case .account(_, let user, _) = kind { return user.email }
        return nil
    }

    /// The cache namespace's account part: the user id, or nil without accounts.
    var accountKey: String? {
        if case .account(_, let user, _) = kind { return "user-\(user.id)" }
        return nil
    }

    var connection: GatewayConnection {
        GatewayConnection(server: server, token: token, account: accountKey)
    }
}

/// Where the session is kept.
protocol SessionStore: Sendable {
    func load() throws -> StoredSession?
    func save(_ session: StoredSession) throws
    func delete() throws
}

/// The session in the Keychain (one generic-password item, this device only, readable after
/// the first unlock so a relaunch in the background can use it). The token is never written
/// anywhere else.
struct KeychainSessionStore: SessionStore {
    var service = "dev.airistotle.reader.session"
    var account = "current"

    struct KeychainError: Error, LocalizedError, Equatable {
        var operation: String
        var status: OSStatus

        var errorDescription: String? {
            let text = SecCopyErrorMessageString(status, nil) as String? ?? "OSStatus \(status)"
            return "The Keychain refused to \(operation) the session: \(text)"
        }
    }

    private var query: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account
        ]
    }

    func load() throws -> StoredSession? {
        var query = query
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else {
            throw KeychainError(operation: "read", status: status)
        }
        return try JSONDecoder().decode(StoredSession.self, from: data)
    }

    func save(_ session: StoredSession) throws {
        let data = try JSONEncoder().encode(session)
        let update: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        ]
        let status = SecItemUpdate(query as CFDictionary, update as CFDictionary)
        if status == errSecItemNotFound {
            var add = query
            add.merge(update) { _, new in new }
            let added = SecItemAdd(add as CFDictionary, nil)
            guard added == errSecSuccess else { throw KeychainError(operation: "save", status: added) }
        } else if status != errSecSuccess {
            throw KeychainError(operation: "save", status: status)
        }
    }

    func delete() throws {
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError(operation: "delete", status: status)
        }
    }
}

import Foundation
import Security

/// Where the Mac bridge lives and the secret it expects.
///
/// Defaults come from the build (`MUSE_BRIDGE_URL` / `MUSE_BRIDGE_SECRET` in
/// Secrets.xcconfig, surfaced through Info.plist) so nothing has to be typed on
/// the watch; the settings screen can override both.
struct BridgeSettings {
    var url: String
    var secret: String

    var baseURL: URL? {
        guard let url = URL(string: url.trimmingCharacters(in: .whitespaces)),
              url.scheme == "http" || url.scheme == "https", url.host != nil else { return nil }
        return url
    }

    var isComplete: Bool { baseURL != nil && !secret.isEmpty }

    private static let urlKey = "bridgeURL"
    private static let secretAccount = "bridgeSecret"

    static func load() -> BridgeSettings {
        let info = Bundle.main.infoDictionary ?? [:]
        let url = UserDefaults.standard.string(forKey: urlKey) ?? (info["MuseBridgeURL"] as? String ?? "")
        let secret = Keychain.read(secretAccount) ?? (info["MuseBridgeSecret"] as? String ?? "")
        return BridgeSettings(url: url, secret: secret)
    }

    func save() {
        UserDefaults.standard.set(url, forKey: Self.urlKey)
        Keychain.write(secret, account: Self.secretAccount)
    }
}

private enum Keychain {
    static func read(_ account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func write(_ value: String, account: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
        var add = query
        add[kSecValueData as String] = Data(value.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(add as CFDictionary, nil)
    }
}

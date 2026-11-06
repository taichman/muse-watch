import AppIntents
import SwiftUI

@main
struct MuseWatchApp: App {
    @StateObject private var model = TalkModel.shared

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
                // The complication opens musewatch://talk, which just shows the app,
                // ready for the user to hold the button.
        }
    }
}

/// "Talk to Muse": for Siri, Shortcuts, and the Apple Watch Ultra Action Button.
struct TalkToMuseIntent: AppIntent {
    static let title: LocalizedStringResource = "Talk to Muse"
    static let description = IntentDescription("Start a hands-free push-to-talk turn with Muse.")
    static let openAppWhenRun = true

    @MainActor
    func perform() async throws -> some IntentResult {
        TalkModel.shared.startHandsFree()
        return .result()
    }
}

struct MuseShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: TalkToMuseIntent(),
            phrases: ["Talk to Muse with \(.applicationName)", "Ask \(.applicationName)"],
            shortTitle: "Talk to Muse",
            systemImageName: "mic.fill"
        )
    }
}

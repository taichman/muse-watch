import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var model: TalkModel
    @State private var pressing = false
    @State private var pressStart = Date()

    var body: some View {
        NavigationStack {
            VStack(spacing: 6) {
                talkButton
                Text(caption)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .lineLimit(2)
                    .minimumScaleFactor(0.8)
            }
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    NavigationLink { HistoryView() } label: { Image(systemName: "clock") }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    NavigationLink { SettingsView() } label: { Image(systemName: "gearshape") }
                }
            }
        }
    }

    private var talkButton: some View {
        ZStack {
            Circle()
                .fill(color.gradient)
                .scaleEffect(model.phase == .listening ? 1 + CGFloat(min(model.level * 4, 0.15)) : 1)
                .animation(.easeOut(duration: 0.1), value: model.level)
            Image(systemName: symbol)
                .font(.system(size: 40, weight: .semibold))
                .foregroundStyle(.white)
                .symbolEffect(.pulse, isActive: model.phase == .thinking)
        }
        .frame(width: 120, height: 120)
        .scaleEffect(pressing ? 0.94 : 1)
        .animation(.spring(duration: 0.2), value: pressing)
        .gesture(
            DragGesture(minimumDistance: 0)
                .onChanged { _ in
                    guard !pressing else { return }
                    pressing = true
                    pressStart = Date()
                    model.pressBegan()
                }
                .onEnded { _ in
                    pressing = false
                    model.pressEnded(heldFor: Date().timeIntervalSince(pressStart))
                }
        )
        .accessibilityLabel("Talk to Muse")
        .accessibilityAddTraits(.isButton)
    }

    private var color: Color {
        switch model.phase {
        case .idle: .indigo
        case .listening: .red
        case .thinking: .orange
        case .speaking: .green
        case .failed: .gray
        }
    }

    private var symbol: String {
        switch model.phase {
        case .idle, .failed: "mic.fill"
        case .listening: "waveform"
        case .thinking: "ellipsis"
        case .speaking: "speaker.wave.2.fill"
        }
    }

    private var caption: String {
        switch model.phase {
        case .idle: "Hold to talk · tap for hands-free"
        case .listening: "Listening…"
        case .thinking: "Muse is thinking"
        case .speaking: "Tap to interrupt"
        case .failed(let message): message
        }
    }
}

struct HistoryView: View {
    @EnvironmentObject private var model: TalkModel

    var body: some View {
        List(model.turns) { turn in
            VStack(alignment: .leading) {
                Text(turn.at, style: .time)
                Text("\(Int(turn.spoke.rounded())) s · \(turn.outcome)")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
        .overlay {
            if model.turns.isEmpty { Text("No turns yet").foregroundStyle(.secondary) }
        }
        .navigationTitle("Recent")
    }
}

struct SettingsView: View {
    @EnvironmentObject private var model: TalkModel
    @State private var url = ""
    @State private var secret = ""
    @State private var check = ""

    var body: some View {
        Form {
            Section("Mac bridge") {
                TextField("http://mac.local:8787", text: $url)
                    .textContentType(.URL)
                SecureField("Secret", text: $secret)
            }
            Section {
                Button("Save & test") {
                    model.save(settings: BridgeSettings(url: url, secret: secret))
                    check = "Checking…"
                    Task { check = await model.checkBridge() }
                }
                if !check.isEmpty { Text(check).foregroundStyle(.secondary) }
            } footer: {
                Text("Run `musebridge secret` on the Mac to see both values.")
            }
        }
        .navigationTitle("Settings")
        .onAppear {
            url = model.settings.url
            secret = model.settings.secret
        }
    }
}

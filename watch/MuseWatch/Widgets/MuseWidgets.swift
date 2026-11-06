import SwiftUI
import WidgetKit

/// Watch-face complication and Smart Stack widget: one tap opens Muse.
@main
struct MuseWidgets: WidgetBundle {
    var body: some Widget {
        TalkComplication()
    }
}

struct TalkComplication: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "MuseTalk", provider: Provider()) { _ in
            TalkComplicationView()
                .containerBackground(for: .widget) { Rectangle().fill(Color.indigo.gradient) }
                .widgetURL(URL(string: "musewatch://talk"))
        }
        .configurationDisplayName("Talk to Muse")
        .description("Opens Muse, ready to talk.")
        .supportedFamilies([.accessoryCircular, .accessoryCorner, .accessoryRectangular, .accessoryInline])
    }

    struct Entry: TimelineEntry {
        let date: Date
    }

    struct Provider: TimelineProvider {
        func placeholder(in context: Context) -> Entry { Entry(date: .now) }
        func getSnapshot(in context: Context, completion: @escaping (Entry) -> Void) {
            completion(Entry(date: .now))
        }
        func getTimeline(in context: Context, completion: @escaping (Timeline<Entry>) -> Void) {
            completion(Timeline(entries: [Entry(date: .now)], policy: .never))
        }
    }
}

struct TalkComplicationView: View {
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        case .accessoryCorner:
            Image(systemName: "waveform")
                .font(.title2)
                .widgetLabel("Muse")
        case .accessoryRectangular:
            HStack {
                Image(systemName: "waveform.circle.fill").font(.title)
                VStack(alignment: .leading) {
                    Text("Muse").font(.headline)
                    Text("Tap to talk").font(.caption).foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
            }
        case .accessoryInline:
            Label("Talk to Muse", systemImage: "waveform")
        default:
            ZStack {
                AccessoryWidgetBackground()
                Image(systemName: "waveform").font(.title2.weight(.semibold))
            }
        }
    }
}

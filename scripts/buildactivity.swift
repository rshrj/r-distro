// R-Distro build activity monitor - a menu-bar view of the running build.
//
// The compiled binary is deliberately not tracked; build it with:
//
//     swiftc -O -parse-as-library scripts/buildactivity.swift -o scripts/buildactivity

import SwiftUI
import AppKit
import Combine
import Foundation

// MARK: - Model

enum BuildState: Equatable {
    case searching
    case running
    case succeeded
    case failed

    var icon: String {
        switch self {
        case .searching:  return "ellipsis"
        case .running:    return "bolt.fill"
        case .succeeded:  return "checkmark"
        case .failed:     return "xmark"
        }
    }

    var title: String {
        switch self {
        case .searching:  return "Waiting for build"
        case .running:    return "Building"
        case .succeeded:  return "Build complete"
        case .failed:     return "Build failed"
        }
    }

    var tint: Color {
        switch self {
        case .searching:  return .secondary
        case .running:    return .cyan
        case .succeeded:  return .green
        case .failed:     return .red
        }
    }
}

struct BuildInfo: Equatable {
    var package = ""
    var done = 0
    var total = 0
    var filename = ""
    var logPath = ""
    var modified = Date.distantPast
    var state: BuildState = .searching

    var progress: Double {
        guard total > 0 else { return 0 }
        return min(max(Double(done) / Double(total), 0), 1)
    }

    var percent: Double {
        progress * 100
    }
}


// MARK: - Build monitor

@MainActor
final class BuildMonitor: ObservableObject {
    @Published var info = BuildInfo()

    private var timer: Timer?

    // Assume this executable is launched from the repository root.
    private let campaignRoot =
        URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        .appendingPathComponent("work/campaigns/gen3-bootstrap")

    init() {
        refresh()

        timer = Timer.scheduledTimer(
            withTimeInterval: 0.4,
            repeats: true
        ) { [weak self] _ in
            Task { @MainActor in
                self?.refresh()
            }
        }

        if let timer {
            RunLoop.main.add(timer, forMode: .common)
        }
    }

    deinit {
        timer?.invalidate()
    }

    func refresh() {
        guard let log = newestBuildLog() else {
            info = BuildInfo()
            return
        }

        guard let text = tail(of: log, bytes: 1_000_000) else {
            return
        }

        var next = BuildInfo()

        next.logPath = log.path
        next.package = packageName(from: log)

        if let attrs = try? FileManager.default.attributesOfItem(
            atPath: log.path
        ) {
            next.modified = attrs[.modificationDate] as? Date ?? .distantPast
        }

        parse(text, into: &next)

        if text.contains("ninja: build stopped: subcommand failed") ||
           text.contains("FAILED:") {
            next.state = .failed
        } else if next.total > 0 && next.done >= next.total {
            next.state = .succeeded
        } else if next.total > 0 {
            next.state = .running
        } else {
            next.state = .searching
        }

        if next != info {
            withAnimation(.easeOut(duration: 0.22)) {
                info = next
            }
        }
    }

    private func newestBuildLog() -> URL? {
        let fm = FileManager.default

        guard let packages = try? fm.contentsOfDirectory(
            at: campaignRoot,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        ) else {
            return nil
        }

        var newestURL: URL?
        var newestDate = Date.distantPast

        for package in packages {
            guard let attempts = try? fm.contentsOfDirectory(
                at: package,
                includingPropertiesForKeys: nil,
                options: [.skipsHiddenFiles]
            ) else {
                continue
            }

            for attempt in attempts
            where attempt.lastPathComponent.hasPrefix("attempt-") {
                let log = attempt.appendingPathComponent("build.log")

                guard fm.fileExists(atPath: log.path),
                      let attrs = try? fm.attributesOfItem(
                          atPath: log.path
                      ),
                      let date = attrs[.modificationDate] as? Date
                else {
                    continue
                }

                if date > newestDate {
                    newestDate = date
                    newestURL = log
                }
            }
        }

        return newestURL
    }

    private func tail(of url: URL, bytes: UInt64) -> String? {
        guard let handle = try? FileHandle(forReadingFrom: url) else {
            return nil
        }

        defer {
            try? handle.close()
        }

        guard let end = try? handle.seekToEnd() else {
            return nil
        }

        let start = end > bytes ? end - bytes : 0

        do {
            try handle.seek(toOffset: start)
            let data = try handle.readToEnd() ?? Data()
            return String(decoding: data, as: UTF8.self)
        } catch {
            return nil
        }
    }

    private func parse(_ text: String, into info: inout BuildInfo) {
        let ninjaProgressPattern = #"\[(\d+)/(\d+)\]"#
        let cmakeProgressPattern = #"\[\s*(\d+)%\]"#

        let sourceFromCompilePattern = #"(?:^|\s)-c\s+(\S+)"#
        let sourceFromObjectPattern =
            #"(?:Building|Generating)\s+(?:CXX|C|Fortran)?\s*object\s+(\S+?)(?:\.o)?(?:\s|$)"#

        guard
            let ninjaProgressRegex = try? NSRegularExpression(
                pattern: ninjaProgressPattern
            ),
            let cmakeProgressRegex = try? NSRegularExpression(
                pattern: cmakeProgressPattern
            ),
            let sourceFromCompileRegex = try? NSRegularExpression(
                pattern: sourceFromCompilePattern
            ),
            let sourceFromObjectRegex = try? NSRegularExpression(
                pattern: sourceFromObjectPattern
            )
        else {
            return
        }

        let lines = text.split(
            separator: "\n",
            omittingEmptySubsequences: true
        )

        for lineSlice in lines.reversed() {
            let line = String(lineSlice)
            let range = NSRange(line.startIndex..., in: line)

            var foundProgress = false

            // --------------------------------------------------
            // Ninja-style: [5563/5951]
            // --------------------------------------------------

            if let match = ninjaProgressRegex.firstMatch(
                in: line,
                range: range
            ) {
                if
                    let doneRange = Range(match.range(at: 1), in: line),
                    let totalRange = Range(match.range(at: 2), in: line)
                {
                    info.done = Int(line[doneRange]) ?? 0
                    info.total = Int(line[totalRange]) ?? 0
                    foundProgress = true
                }
            }

            // --------------------------------------------------
            // CMake-style: [ 57%]
            // Represent as 57 / 100.
            // --------------------------------------------------

            if !foundProgress,
            let match = cmakeProgressRegex.firstMatch(
                    in: line,
                    range: range
            ),
            let pctRange = Range(match.range(at: 1), in: line) {

                let pct = Int(line[pctRange]) ?? 0

                info.done = pct
                info.total = 100
                foundProgress = true
            }

            guard foundProgress else {
                continue
            }

            // --------------------------------------------------
            // Preferred filename extraction:
            //
            //   -c /path/to/Foo.cc
            // --------------------------------------------------

            if let source = sourceFromCompileRegex.firstMatch(
                in: line,
                range: range
            ),
            let sourceRange = Range(source.range(at: 1), in: line) {

                let path = String(line[sourceRange])
                info.filename = URL(
                    fileURLWithPath: path
                ).lastPathComponent
            }

            // --------------------------------------------------
            // Fallback for shorter CMake lines:
            //
            // [ 57%] Building CXX object .../Foo.cc.o
            // --------------------------------------------------

            else if let source = sourceFromObjectRegex.firstMatch(
                in: line,
                range: range
            ),
                    let sourceRange = Range(
                        source.range(at: 1),
                        in: line
                    ) {

                var path = String(line[sourceRange])

                if path.hasSuffix(".o") {
                    path.removeLast(2)
                }

                info.filename = URL(
                    fileURLWithPath: path
                ).lastPathComponent
            }

            break
        }
    }

    private func packageName(from log: URL) -> String {
        // .../<package>/attempt-X/build.log
        log
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .lastPathComponent
    }

    func revealLog() {
        guard !info.logPath.isEmpty else { return }

        NSWorkspace.shared.activateFileViewerSelecting([
            URL(fileURLWithPath: info.logPath)
        ])
    }

    func quit() {
        NSApplication.shared.terminate(nil)
    }
}


// MARK: - Progress bar

struct ModernProgressBar: View {
    let progress: Double
    let tint: Color

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .leading) {
                Capsule()
                    .fill(.primary.opacity(0.08))

                Capsule()
                    .fill(
                        LinearGradient(
                            colors: [
                                tint.opacity(0.75),
                                tint
                            ],
                            startPoint: .leading,
                            endPoint: .trailing
                        )
                    )
                    .frame(
                        width: max(
                            0,
                            geometry.size.width * progress
                        )
                    )
                    .shadow(
                        color: tint.opacity(0.28),
                        radius: 5,
                        y: 1
                    )
            }
        }
        .frame(height: 8)
        .animation(
            .easeOut(duration: 0.28),
            value: progress
        )
    }
}


// MARK: - Status icon

struct StatusOrb: View {
    let state: BuildState

    var body: some View {
        ZStack {
            Circle()
                .fill(state.tint.opacity(0.12))

            Circle()
                .stroke(
                    state.tint.opacity(0.18),
                    lineWidth: 1
                )

            Image(systemName: state.icon)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(state.tint)
        }
        .frame(width: 38, height: 38)
    }
}


// MARK: - Main panel

struct BuildPanel: View {
    @ObservedObject var monitor: BuildMonitor

    private var info: BuildInfo {
        monitor.info
    }

    var body: some View {
        VStack(spacing: 0) {

            header

            Divider()
                .opacity(0.6)
                .padding(.vertical, 14)

            if info.state == .searching {
                waitingView
            } else {
                buildView
            }

            Divider()
                .opacity(0.6)
                .padding(.vertical, 13)

            footer
        }
        .padding(16)
        .frame(width: 370)
    }

    private var header: some View {
        HStack(spacing: 11) {
            StatusOrb(state: info.state)

            VStack(alignment: .leading, spacing: 2) {
                Text(info.state.title)
                    .font(.system(size: 15, weight: .semibold))

                Text(
                    info.package.isEmpty
                    ? "R-Distro"
                    : info.package
                )
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.middle)
            }

            Spacer()

            if info.total > 0 {
                Text(
                    String(
                        format: "%.1f%%",
                        info.percent
                    )
                )
                .font(
                    .system(
                        size: 22,
                        weight: .bold,
                        design: .rounded
                    )
                )
                .monospacedDigit()
                .contentTransition(.numericText())
            }
        }
    }

    private var buildView: some View {
        VStack(alignment: .leading, spacing: 14) {

            ModernProgressBar(
                progress: info.progress,
                tint: info.state.tint
            )

            HStack {
                Text("\(info.done) / \(info.total)")
                    .font(
                        .system(
                            size: 11,
                            weight: .medium,
                            design: .monospaced
                        )
                    )
                    .foregroundStyle(.secondary)
                    .monospacedDigit()

                Spacer()

                Text(relativeDate(info.modified))
                    .font(.system(size: 11))
                    .foregroundStyle(.tertiary)
            }

            if !info.filename.isEmpty {
                HStack(spacing: 10) {
                    Image(systemName: "doc.text")
                        .font(.system(size: 13))
                        .foregroundStyle(info.state.tint)
                        .frame(width: 18)

                    VStack(alignment: .leading, spacing: 2) {
                        Text(
                            info.state == .running
                            ? "Compiling"
                            : "Last file"
                        )
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(.secondary)
                        .textCase(.uppercase)

                        Text(info.filename)
                            .font(
                                .system(
                                    size: 13,
                                    weight: .medium,
                                    design: .monospaced
                                )
                            )
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }

                    Spacer()
                }
                .padding(.horizontal, 11)
                .padding(.vertical, 10)
                .background(
                    RoundedRectangle(cornerRadius: 10)
                        .fill(.primary.opacity(0.045))
                )
            }
        }
    }

    private var waitingView: some View {
        VStack(spacing: 10) {
            Image(systemName: "waveform.path")
                .font(.system(size: 27, weight: .light))
                .foregroundStyle(.secondary)

            Text("Waiting for build activity")
                .font(.system(size: 13, weight: .medium))

            Text("Watching gen3-bootstrap build logs")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 13)
    }

    private var footer: some View {
        HStack(spacing: 8) {
            Button {
                monitor.revealLog()
            } label: {
                Label(
                    "Reveal Log",
                    systemImage: "folder"
                )
            }
            .disabled(info.logPath.isEmpty)

            Spacer()

            Button {
                monitor.quit()
            } label: {
                Image(systemName: "power")
            }
            .help("Quit Build Activity")
        }
        .font(.system(size: 12))
        .buttonStyle(.borderless)
        .foregroundStyle(.secondary)
    }

    private func relativeDate(_ date: Date) -> String {
        guard date != .distantPast else {
            return ""
        }

        let seconds = Int(
            Date().timeIntervalSince(date)
        )

        switch seconds {
        case ..<2:
            return "just now"
        case 2..<60:
            return "\(seconds)s ago"
        case 60..<3600:
            return "\(seconds / 60)m ago"
        default:
            return "\(seconds / 3600)h ago"
        }
    }
}


// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(
        _ notification: Notification
    ) {
        // Menu-bar-only application: no Dock icon.
        NSApplication.shared.setActivationPolicy(.accessory)
    }
}

@main
struct RDistBuildActivityApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self)
    private var appDelegate

    @StateObject
    private var monitor = BuildMonitor()

    var body: some Scene {
        MenuBarExtra {
            BuildPanel(monitor: monitor)
        } label: {
            HStack(spacing: 4) {
                Image(
                    systemName: monitor.info.state.icon
                )

                if monitor.info.total > 0 {
                    Text(
                        String(
                            format: "%.1f%%",
                            monitor.info.percent
                        )
                    )
                    .monospacedDigit()
                }
            }
        }
        .menuBarExtraStyle(.window)
    }
}
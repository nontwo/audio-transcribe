import Foundation

struct Recording {
    let id = UUID()
    let url: URL
    var state = "waiting"
    var message: String? = nil
}

final class RecordingQueue {
    var items: [Recording] = []
    func add(_ urls: [URL]) {
        let ordered = urls.filter { $0.isFileURL }.enumerated().sorted { a, b in
            let result = a.element.lastPathComponent.compare(b.element.lastPathComponent,
                options: [.numeric, .caseInsensitive], locale: Locale(identifier: "en_US_POSIX"))
            if result != .orderedSame { return result == .orderedAscending }
            if a.element.path != b.element.path { return a.element.path < b.element.path }
            return a.offset < b.offset
        }
        items += ordered.map { Recording(url: $0.element.standardizedFileURL) }
    }
    func move(_ indexes: IndexSet, to destination: Int) {
        guard !indexes.isEmpty, indexes.allSatisfy({ items.indices.contains($0) }),
              destination >= 0, destination <= items.count else { return }
        let moving = indexes.map { items[$0] }
        let insertion = destination - indexes.filter { $0 < destination }.count
        for i in indexes.reversed() { items.remove(at: i) }
        items.insert(contentsOf: moving, at: insertion)
    }
}

enum QueuePersistence {
    static let states: Set<String> = ["waiting", "checking", "processing", "completed", "review_required", "failed", "cancelled"]

    static func snapshot(_ items: [Recording]) -> [[String: String]] {
        items.map { item in
            var value = ["path": item.url.path, "state": item.state]
            if let message = item.message { value["message"] = message }
            return value
        }
    }

    static func reportMatches(_ paths: [String], entries: [[String: Any]]) -> Bool {
        !paths.isEmpty && entries.count == paths.count && paths.enumerated().allSatisfy {
            entries[$0.offset]["selected_path"] as? String == $0.element
        }
    }

    static func savedTaskFinished(_ paths: [String], saved: [[String: String]], markedFinished: Bool) -> Bool {
        markedFinished && !paths.isEmpty && saved.count == paths.count && paths.enumerated().allSatisfy {
            saved[$0.offset]["path"] == $0.element && ["completed", "review_required", "failed"].contains(saved[$0.offset]["state"] ?? "")
        }
    }

    static func restore(_ paths: [String], saved: [[String: String]], reportEntries: [[String: Any]] = []) -> [Recording] {
        paths.enumerated().map { index, path in
            var item = Recording(url: URL(fileURLWithPath: path))
            if saved.indices.contains(index), saved[index]["path"] == path {
                item.state = saved[index]["state"] ?? "waiting"
                item.message = saved[index]["message"]
            } else if reportEntries.indices.contains(index), reportEntries[index]["selected_path"] as? String == path {
                item.state = reportEntries[index]["state"] as? String ?? "waiting"
            }
            if !states.contains(item.state) { item.state = "waiting"; item.message = nil }
            if ["checking", "processing"].contains(item.state) {
                item.state = "waiting"
                item.message = "上次处理已中断，点击开始转录继续。"
            }
            return item
        }
    }
}

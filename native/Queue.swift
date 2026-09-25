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

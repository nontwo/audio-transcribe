import Foundation

@main struct TaskTests {
    static func main() {
        let paths = ["/tmp/synthetic-a.wav", "/tmp/synthetic-b.wav"]
        let saved = [["path": paths[0], "state": "processing"], ["path": paths[1], "state": "review_required", "message": "Synthetic review"]]
        let restored = QueuePersistence.restore(paths, saved: saved)
        assert(restored[0].state == "waiting" && restored[0].message != nil)
        assert(restored[1].state == "review_required" && restored[1].message == "Synthetic review")
        let copy = QueuePersistence.restore(paths, saved: QueuePersistence.snapshot(restored))
        assert(copy.map { $0.state } == restored.map { $0.state })
        let migrated = QueuePersistence.restore(paths, saved: [], reportEntries: [
            ["selected_path": paths[0], "state": "review_required"], ["selected_path": paths[1], "state": "completed"]])
        assert(migrated.map { $0.state } == ["review_required", "completed"])
        let changed = QueuePersistence.restore(Array(paths.reversed()), saved: saved)
        assert(changed.allSatisfy { $0.state == "waiting" })
        let unknown = QueuePersistence.restore([paths[0]], saved: [["path": paths[0], "state": "untrusted-state"]])
        assert(unknown[0].state == "waiting")
        let entries: [[String: Any]] = [["selected_path": paths[0]], ["selected_path": paths[1]]]
        assert(QueuePersistence.reportMatches(paths, entries: entries))
        assert(!QueuePersistence.reportMatches(Array(paths.reversed()), entries: entries))
        assert(!QueuePersistence.reportMatches(paths, entries: [entries[0]]))
        let finished = QueuePersistence.snapshot(migrated)
        assert(QueuePersistence.savedTaskFinished(paths, saved: finished, markedFinished: true))
        assert(!QueuePersistence.savedTaskFinished(paths, saved: saved, markedFinished: true))
        assert(!QueuePersistence.savedTaskFinished(Array(paths.reversed()), saved: finished, markedFinished: true))
        print("Task persistence tests passed: resumed work stays pending; completed/review states survive reopening; legacy report matching is exact.")
    }
}

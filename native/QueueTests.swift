import Foundation

@main struct QueueTests {
    static func main() {
        let queue = RecordingQueue()
        let a = URL(fileURLWithPath: "/fixtures/lecture2 空间.wav")
        let b = URL(fileURLWithPath: "/fixtures/lecture10 空间.mp3")
        queue.add([b,a,a])
        assert(queue.items.map { $0.url } == [a,a,b])
        assert(Set(queue.items.map { $0.id }).count == 3)
        queue.move(IndexSet(integer:2), to:0)
        assert(queue.items.map { $0.url } == [b,a,a])
        queue.add([URL(fileURLWithPath:"/fixtures/lecture1.m4a")])
        assert(queue.items[0].url == b) // Adding files does not undo confirmed order.
        queue.move(IndexSet([0,2]),to:4)
        assert(queue.items.map { $0.url } == [a,URL(fileURLWithPath:"/fixtures/lecture1.m4a"),b,a])
        let previous = queue.items.map { $0.id }
        queue.add([]); queue.move(IndexSet(integer:99),to:0)
        assert(queue.items.map { $0.id } == previous)
        var progress = TranscriptionProgress(started: 100)
        assert(!progress.detail.contains("%"))
        progress.update(["stage": "transcribing"])
        assert(progress.detail == "Starting transcription…")
        progress.update(["stage": "transcribing", "percent": 35])
        assert(progress.detail == "Transcribing 35%")
        progress.update(["stage": "transcribing", "percent": NSNull()])
        progress.update(["stage": "transcribing", "percent": 10])
        assert(progress.percent == 35)
        let status = progress.status(index: 1, total: 3, now: 100 + 3665)
        assert(status.contains("Processing 2 of 3") && status.contains("Elapsed 1:01:05"))
        progress.update(["stage": "validating"])
        assert(progress.percent == nil && progress.detail == "Validating transcript…")
        assert(!progress.status(index: 0, total: 1, now: 0).contains("-"))
        var groups = ClassGrouping(recordings: [["date": "2026-01-02"], ["date": "2026-01-02"], [:], ["date": "2026-01-03"]],
            suggestions: [["indices": [0, 1], "title": "Morning class"], ["indices": [2], "title": "Unknown date"], ["indices": [3], "title": "Next day"]])
        assert(groups.groups.map { $0["indices"] as! [Int] } == [[0, 1], [2], [3]])
        assert(groups.groups[0]["date"] as? String == "2026-01-02")
        assert(groups.groups[1]["date"] == nil)
        groups.starts[1] = true; groups.titles[1] = " "
        assert(groups.groups.map { $0["indices"] as! [Int] } == [[0], [1], [2], [3]])
        assert(groups.groups[1]["title"] as? String == "Class recordings")
        groups.starts[2] = false; groups.starts[3] = false
        assert(groups.groups.map { $0["indices"] as! [Int] } == [[0], [1, 2, 3]])
        assert(groups.groups[1]["date"] == nil) // Never assign a known date to a mixed/unknown group.
        groups.explicitDates[1] = "2026-02-28"
        assert(groups.groups[1]["date"] as? String == "2026-02-28")
        groups.explicitDates[0] = ""
        assert(groups.groups[0]["date"] is NSNull)
        assert(ClassGrouping.validDate("2024-02-29") && ClassGrouping.validDate(""))
        assert(!ClassGrouping.validDate("2026-02-29") && !ClassGrouping.validDate("2026-2-1"))
        assert(groups.groups.allSatisfy { $0["confirmed"] as? Bool == true })
        assert(ClassGrouping(recordings: [], suggestions: []).groups.isEmpty)
        let reportBody = "# Synthetic class\n\n## 1. fixture.wav\n\n**REVIEW REQUIRED — retain this warning.**\n\n[-00:00:00.100 – 00:00:01.000] Synthetic text.\n\n## 2. missing.wav\n\n**FAILED — retain this warning.**\n"
        let report = "# Transcript Report\n\nSelected files: 2\nTimestamps restart at zero for each source.\nAll stored ASR text is retained.\n\n" + reportBody
        assert(ReportPresentation.readingBody(report, integrity: "intact") == reportBody)
        assert(ReportPresentation.readingBody(report, integrity: "modified") == nil)
        assert(ReportPresentation.readingBody(report, integrity: "unknown") == nil)
        assert(ReportPresentation.readingBody("# Unfamiliar report\n\n## Source\nPreserve everything.", integrity: "intact") == nil)
        // Imported older recordings are ordered by the backend's generated time;
        // rendering must compare actual instants, not mislabel UTC as local time.
        let generated = LibraryDisplayText.generatedTime("2024-01-02T09:00:00Z")
        assert(generated == LibraryDisplayText.generatedTime("2024-01-02T04:00:00-05:00"))
        assert(generated == LibraryDisplayText.generatedTime("2024-01-02T09:00:00.000000+00:00"))
        assert(generated != "生成时间未知")
        assert(LibraryDisplayText.generatedTime(nil) == "生成时间未知")
        assert(LibraryDisplayText.generatedTime("unknown") == "生成时间未知")
        assert(LibraryDisplayText.filenames(["fixture.wav"]) == "fixture.wav")
        assert(LibraryDisplayText.filenames(["fixture.wav", "second.wav"]).contains("2 个文件"))
        print("Queue tests passed: deterministic additions, duplicates, multiple-row reorder, invalid moves, cancellation/no-op.")
        print("Progress tests passed: no invented percentage, stage transitions, monotonic updates and multi-hour elapsed time.")
        print("Grouping tests passed: explicit boundaries preserve every index, split/merge, title fallback, and mixed/unknown dates.")
        print("Reader tests passed: only known intact preambles compact; all source warnings and text retained; edited/unknown reports remain full.")
        print("Result display tests passed: UTC/offset/fractional export times identify the same local time; unknown dates and filenames remain explicit.")
    }
}

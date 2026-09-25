import Foundation

struct TranscriptionProgress {
    var stage = "checking"
    var percent: Int?
    let started: TimeInterval

    mutating func update(_ event: [String: Any]) {
        guard let next = event["stage"] as? String,
              ["preparing", "waiting_for_engine", "transcribing", "validating"].contains(next) else { return }
        if stage != next { percent = nil }
        stage = next
        if let value = event["percent"] as? Int, (0...100).contains(value) {
            percent = max(percent ?? 0, value)
        }
    }
    var detail: String {
        switch stage {
        case "preparing": return "Preparing audio…"
        case "waiting_for_engine": return "Waiting for transcription engine…"
        case "transcribing": return percent.map { "Transcribing \($0)%" } ?? "Starting transcription…"
        case "validating": return "Validating transcript…"
        default: return "Checking recording…"
        }
    }
    func status(index: Int, total: Int, now: TimeInterval) -> String {
        let seconds = Int(max(0, now - started))
        let elapsed = seconds >= 3600
            ? String(format: "%d:%02d:%02d", seconds / 3600, seconds / 60 % 60, seconds % 60)
            : String(format: "%02d:%02d", seconds / 60, seconds % 60)
        return "Processing \(index + 1) of \(total) · \(detail) · Elapsed \(elapsed)\nThe report appears when all recordings finish."
    }
}

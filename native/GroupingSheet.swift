import Cocoa

final class GroupingSheet: NSWindowController {
    var grouping: ClassGrouping
    let recordings: [[String: Any]]
    let suggestions: [[String: Any]]
    var completion: (([[String: Any]]?) -> Void)?
    var titleFields: [NSTextField] = []
    var boundaryButtons: [NSButton] = []
    var dateFields: [NSTextField] = []

    init(plan: [String: Any]) {
        recordings = plan["recordings"] as? [[String: Any]] ?? []
        suggestions = plan["groups"] as? [[String: Any]] ?? []
        grouping = ClassGrouping(recordings: recordings, suggestions: suggestions)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 800, height: 570),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.title = "Review Class Groups"
        super.init(window: window)
        buildView()
    }
    required init?(coder: NSCoder) { fatalError() }

    func buildView() {
        guard let content = window?.contentView else { return }
        let root = NSStackView(); root.orientation = .vertical; root.alignment = .leading; root.spacing = 14
        root.translatesAutoresizingMaskIntoConstraints = false; content.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 22),
            root.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -22),
            root.topAnchor.constraint(equalTo: content.topAnchor, constant: 20),
            root.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -20)])
        let heading = NSTextField(labelWithString: "Which recordings belong to the same class?")
        heading.font = .systemFont(ofSize: 19, weight: .semibold); root.addArrangedSubview(heading)
        let explanation = NSTextField(wrappingLabelWithString: "Suggestions use recording dates, start times and measured duration. They cannot identify a course. Check “Start new class” to split; uncheck it to join the class above. Correct the class date if needed, or leave it blank when unknown. File order stays unchanged.")
        explanation.textColor = .secondaryLabelColor; root.addArrangedSubview(explanation)
        explanation.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        let scroll = NSScrollView(); scroll.hasVerticalScroller = true; scroll.borderType = .bezelBorder
        let rows = NSStackView(); rows.orientation = .vertical; rows.alignment = .leading; rows.spacing = 0
        rows.translatesAutoresizingMaskIntoConstraints = false; scroll.documentView = rows
        root.addArrangedSubview(scroll)
        NSLayoutConstraint.activate([scroll.widthAnchor.constraint(equalTo: root.widthAnchor),
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 240),
            rows.leadingAnchor.constraint(equalTo: scroll.contentView.leadingAnchor),
            rows.trailingAnchor.constraint(equalTo: scroll.contentView.trailingAnchor),
            rows.topAnchor.constraint(equalTo: scroll.contentView.topAnchor)])
        for i in recordings.indices {
            let record = recordings[i]
            let row = NSStackView(); row.orientation = .vertical; row.alignment = .leading; row.spacing = 6
            row.edgeInsets = NSEdgeInsets(top: 12, left: 12, bottom: 12, right: 12)
            let name = NSTextField(labelWithString: "\(i + 1). \(record["filename"] as? String ?? "Recording")")
            name.font = .systemFont(ofSize: 13, weight: .medium); name.lineBreakMode = .byTruncatingMiddle
            row.addArrangedSubview(name)
            let date = record["date"] as? String ?? "Unknown date"
            let time = record["time"] as? String ?? "Unknown start time"
            var detail = "\(date) · \(time)"
            if let duration = record["duration_seconds"] as? Double { detail += " · \(Int(duration / 60))m \(Int(duration) % 60)s" }
            let metadata = NSTextField(labelWithString: detail); metadata.font = .systemFont(ofSize: 11)
            metadata.textColor = .secondaryLabelColor; row.addArrangedSubview(metadata)
            if let suggestion = suggestions.first(where: { ($0["indices"] as? [Int])?.first == i }) {
                if let reason = suggestion["reason"] as? String {
                    let note = NSTextField(wrappingLabelWithString: reason)
                    note.font = .systemFont(ofSize: 11); note.textColor = .secondaryLabelColor; row.addArrangedSubview(note)
                    note.widthAnchor.constraint(equalTo: row.widthAnchor, constant: -24).isActive = true
                }
                for issue in suggestion["issues"] as? [String] ?? [] {
                    let note = NSTextField(wrappingLabelWithString: "Review: " + issue)
                    note.font = .systemFont(ofSize: 11); note.textColor = .systemOrange; row.addArrangedSubview(note)
                    note.widthAnchor.constraint(equalTo: row.widthAnchor, constant: -24).isActive = true
                }
            }
            let boundary = NSButton(checkboxWithTitle: "Start new class", target: self, action: #selector(changeBoundary(_:)))
            boundary.tag = i; boundary.state = grouping.starts[i] ? .on : .off; boundary.isEnabled = i != 0
            boundary.setAccessibilityLabel("Recording \(i + 1): Start new class")
            let title = NSTextField(string: grouping.titles[i]); title.placeholderString = "Class title"
            title.isEnabled = grouping.starts[i]; title.setAccessibilityLabel("Recording \(i + 1): Class title")
            let dateField = NSTextField(string: grouping.dates[i] ?? "")
            dateField.placeholderString = "YYYY-MM-DD"; dateField.isEnabled = grouping.starts[i]
            dateField.setAccessibilityLabel("Recording \(i + 1): Class date, YYYY-MM-DD, blank if unknown")
            dateField.widthAnchor.constraint(equalToConstant: 110).isActive = true
            let controls = NSStackView(views: [boundary, title, dateField]); controls.spacing = 12
            row.addArrangedSubview(controls)
            controls.widthAnchor.constraint(equalTo: row.widthAnchor, constant: -24).isActive = true
            titleFields.append(title); boundaryButtons.append(boundary)
            dateFields.append(dateField)
            rows.addArrangedSubview(row); row.widthAnchor.constraint(equalTo: rows.widthAnchor).isActive = true
            let separator = NSBox(); separator.boxType = .separator; rows.addArrangedSubview(separator)
            separator.widthAnchor.constraint(equalTo: rows.widthAnchor).isActive = true
        }
        let cancel = NSButton(title: "Back", target: self, action: #selector(cancelSheet)); cancel.bezelStyle = .rounded
        let confirm = NSButton(title: "Confirm Groups & Transcribe", target: self, action: #selector(confirmSheet))
        confirm.bezelStyle = .rounded; confirm.keyEquivalent = "\r"; confirm.isEnabled = !recordings.isEmpty
        let actions = NSStackView(views: [NSView(), cancel, confirm]); actions.spacing = 8
        root.addArrangedSubview(actions); actions.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
    }
    @objc func changeBoundary(_ button: NSButton) {
        grouping.starts[button.tag] = button.state == .on
        titleFields[button.tag].isEnabled = grouping.starts[button.tag]
        dateFields[button.tag].isEnabled = grouping.starts[button.tag]
    }
    @objc func cancelSheet() { finish(nil) }
    @objc func confirmSheet() {
        grouping.titles = titleFields.map { $0.stringValue }
        for i in recordings.indices where grouping.starts[i] {
            let date = dateFields[i].stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            guard ClassGrouping.validDate(date) else {
                let alert = NSAlert(); alert.messageText = "Check the class date"
                alert.informativeText = "Recording \(i + 1): enter a real date as YYYY-MM-DD, or leave the date blank if it is unknown."
                alert.runModal(); window?.makeFirstResponder(dateFields[i]); return
            }
            let title = grouping.titles[i].trimmingCharacters(in: .whitespacesAndNewlines)
            guard title.count <= 160 else {
                let alert = NSAlert(); alert.messageText = "Use a shorter class title"
                alert.informativeText = "Class titles can contain up to 160 characters."
                alert.runModal(); window?.makeFirstResponder(titleFields[i]); return
            }
            grouping.explicitDates[i] = date
        }
        // Joining different filename dates is possible, but requires an explicit second decision.
        let crossesDates = grouping.groups.contains { group in
            let indices = group["indices"] as? [Int] ?? []
            return Set(indices.compactMap { grouping.dates[$0] }).count > 1
        }
        if crossesDates {
            let alert = NSAlert(); alert.messageText = "Join recordings from different dates?"
            alert.informativeText = "At least one class contains recordings with different original dates. Confirm that this is the intended class and that its displayed date is correct."
            alert.addButton(withTitle: "Join Dates"); alert.addButton(withTitle: "Keep Editing")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
        }
        finish(grouping.groups)
    }
    func finish(_ groups: [[String: Any]]?) {
        if let window, let parent = window.sheetParent { parent.endSheet(window); window.orderOut(nil) }
        completion?(groups)
    }
}

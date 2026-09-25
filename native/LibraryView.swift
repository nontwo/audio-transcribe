import Cocoa

private struct LibraryRow {
    var heading: String?
    var report: [String: Any]?
}

final class LibraryController: NSViewController, NSTableViewDataSource, NSTableViewDelegate, NSSearchFieldDelegate {
    let client = LibraryClient()
    let table = NSTableView()
    let search = NSSearchField()
    let titleLabel = NSTextField(labelWithString: "Your transcript library")
    let detailLabel = NSTextField(wrappingLabelWithString: "Reports appear here after transcription.")
    let transcript = NSTextView()
    let sourcePicker = NSPopUpButton()
    let openAudio = NSButton(title: "Open Audio", target: nil, action: nil)
    let reportDetails = NSButton(checkboxWithTitle: "Report details", target: nil, action: nil)
    let rename = NSButton(title: "Rename…", target: nil, action: nil)
    let regroup = NSButton(title: "Review Groups…", target: nil, action: nil)
    let open = NSButton(title: "Open Report", target: nil, action: nil)
    let reveal = NSButton(title: "Show in Finder", target: nil, action: nil)
    let message = NSTextField(wrappingLabelWithString: "Loading library…")
    var onRequeue: (([URL]) -> Void)?
    var canRequeue = true { didSet { updateActions() } }
    private var rows: [LibraryRow] = []
    private var selectedReport: [String: Any]?
    private var sources: [[String: Any]] = []
    private var listGeneration = 0
    private var readGeneration = 0
    private var searchTimer: Timer?
    private var pendingPath: String?
    private var fullMarkdown = ""
    private var readingMarkdown: String?
    var currentPath: String? { selectedReport?["report"] as? String }

    override func loadView() {
        view = NSView()
        let root = NSStackView(); root.orientation = .vertical; root.alignment = .leading; root.spacing = 12
        root.translatesAutoresizingMaskIntoConstraints = false; view.addSubview(root)
        NSLayoutConstraint.activate([root.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 18),
            root.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -18),
            root.topAnchor.constraint(equalTo: view.topAnchor, constant: 18),
            root.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -16)])
        let heading = NSTextField(labelWithString: "Library"); heading.font = .systemFont(ofSize: 23, weight: .semibold)
        search.placeholderString = "Search reports and transcripts"; search.delegate = self
        search.setAccessibilityLabel("Search library")
        let reload = NSButton(title: "Refresh", target: self, action: #selector(reloadLibrary)); reload.bezelStyle = .rounded
        let toolbar = NSStackView(views: [heading, NSView(), search, reload]); toolbar.spacing = 12
        root.addArrangedSubview(toolbar); toolbar.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        search.widthAnchor.constraint(equalToConstant: 320).isActive = true
        let split = NSSplitView(); split.isVertical = true; split.dividerStyle = .thin
        root.addArrangedSubview(split); split.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        split.heightAnchor.constraint(greaterThanOrEqualToConstant: 380).isActive = true
        split.setContentHuggingPriority(.defaultLow, for: .vertical)
        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("report"))
        column.minWidth = 200; column.width = 270; table.addTableColumn(column)
        table.headerView = nil; table.style = .sourceList; table.delegate = self; table.dataSource = self
        table.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle; table.setAccessibilityLabel("Reports by recording date")
        let listScroll = NSScrollView(); listScroll.documentView = table; listScroll.hasVerticalScroller = true
        listScroll.translatesAutoresizingMaskIntoConstraints = false
        split.addArrangedSubview(listScroll); listScroll.widthAnchor.constraint(greaterThanOrEqualToConstant: 220).isActive = true
        let reader = NSStackView(); reader.orientation = .vertical; reader.alignment = .leading; reader.spacing = 10
        reader.edgeInsets = NSEdgeInsets(top: 0, left: 16, bottom: 0, right: 0)
        split.addArrangedSubview(reader); reader.widthAnchor.constraint(greaterThanOrEqualToConstant: 470).isActive = true
        titleLabel.font = .systemFont(ofSize: 19, weight: .semibold); titleLabel.lineBreakMode = .byTruncatingTail
        reader.addArrangedSubview(titleLabel); titleLabel.widthAnchor.constraint(equalTo: reader.widthAnchor, constant: -16).isActive = true
        detailLabel.font = .systemFont(ofSize: 12); detailLabel.textColor = .secondaryLabelColor
        reader.addArrangedSubview(detailLabel); detailLabel.widthAnchor.constraint(equalTo: titleLabel.widthAnchor).isActive = true
        sourcePicker.target = self; sourcePicker.action = #selector(sourceChanged)
        sourcePicker.setAccessibilityLabel("Source recording")
        openAudio.target = self; openAudio.action = #selector(openSource); openAudio.bezelStyle = .rounded
        reportDetails.target = self; reportDetails.action = #selector(toggleReportDetails)
        reportDetails.toolTip = "Show the complete generated report introduction and metadata. Open Report always opens the full file."
        reportDetails.isHidden = true
        let sourceBar = NSStackView(views: [sourcePicker, openAudio, reportDetails]); sourceBar.spacing = 8
        reader.addArrangedSubview(sourceBar); sourceBar.widthAnchor.constraint(equalTo: titleLabel.widthAnchor).isActive = true
        sourcePicker.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let textScroll = NSScrollView(); textScroll.hasVerticalScroller = true; textScroll.borderType = .bezelBorder
        transcript.isEditable = false; transcript.isSelectable = true; transcript.isRichText = true
        transcript.usesFindBar = true; transcript.isAutomaticLinkDetectionEnabled = false
        transcript.textContainerInset = NSSize(width: 18, height: 16)
        transcript.minSize = NSSize(width: 0, height: 0); transcript.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        transcript.isVerticallyResizable = true; transcript.isHorizontallyResizable = false
        transcript.autoresizingMask = [.width]; transcript.textContainer?.widthTracksTextView = true
        transcript.setAccessibilityLabel("Transcript report")
        textScroll.documentView = transcript; reader.addArrangedSubview(textScroll)
        textScroll.widthAnchor.constraint(equalTo: titleLabel.widthAnchor).isActive = true
        textScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 220).isActive = true
        textScroll.setContentHuggingPriority(.defaultLow, for: .vertical)
        for button in [rename, regroup, open, reveal] { button.target = self; button.bezelStyle = .rounded }
        rename.action = #selector(renameReport); regroup.action = #selector(requeueReport)
        open.action = #selector(openReport); reveal.action = #selector(revealReport)
        let actions = NSStackView(views: [rename, regroup, NSView(), open, reveal]); actions.spacing = 6
        reader.addArrangedSubview(actions); actions.widthAnchor.constraint(equalTo: titleLabel.widthAnchor).isActive = true
        message.font = .systemFont(ofSize: 11); message.textColor = .secondaryLabelColor
        root.addArrangedSubview(message); message.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        updateActions()
        DispatchQueue.main.async { split.setPosition(270, ofDividerAt: 0) }
    }

    func reload(selectPath: String? = nil) {
        _ = view
        pendingPath = selectPath ?? currentPath
        listGeneration += 1; let generation = listGeneration
        message.stringValue = "Loading library…"
        client.request(["action": "list", "query": search.stringValue]) { [weak self] result in
            guard let self, generation == self.listGeneration else { return }
            switch result {
            case .failure:
                self.message.stringValue = "Could not load the local library. Click Refresh to retry."
            case .success(let value):
                self.rows = []
                for date in value["dates"] as? [[String: Any]] ?? [] {
                    let reports = date["reports"] as? [[String: Any]] ?? []
                    guard !reports.isEmpty else { continue }
                    self.rows.append(LibraryRow(heading: date["label"] as? String ?? date["date"] as? String ?? "Unknown recording date"))
                    self.rows += reports.map { LibraryRow(report: $0) }
                }
                self.table.reloadData()
                let index = self.rows.firstIndex { $0.report?["report"] as? String == self.pendingPath && self.pendingPath != nil }
                    ?? self.rows.firstIndex { $0.report != nil }
                if let index {
                    if self.table.selectedRow == index { self.readReport(at: index) }
                    else { self.table.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false) }
                }
                else { self.clearReader() }
                let count = self.rows.filter { $0.report != nil }.count
                self.message.stringValue = count == 0 ? (self.search.stringValue.isEmpty ? "No reports yet. Choose New Transcription to begin." : "No matching reports.") : "Organized by recording date and start time. Dates come from recording metadata, not transcript content."
            }
        }
    }
    @objc func reloadLibrary() { reload() }
    func controlTextDidChange(_ notification: Notification) {
        searchTimer?.invalidate()
        searchTimer = Timer.scheduledTimer(withTimeInterval: 0.3, repeats: false) { [weak self] _ in self?.reload() }
    }
    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }
    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat { rows[row].heading == nil ? 65 : 31 }
    func tableView(_ tableView: NSTableView, shouldSelectRow row: Int) -> Bool { rows[row].report != nil }
    func tableView(_ tableView: NSTableView, isGroupRow row: Int) -> Bool { rows[row].heading != nil }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let cell = NSTableCellView()
        let title = NSTextField(labelWithString: rows[row].heading ?? rows[row].report?["title"] as? String ?? "Transcript report")
        title.font = .systemFont(ofSize: 12, weight: .semibold); title.lineBreakMode = .byTruncatingTail
        title.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(title); cell.textField = title
        NSLayoutConstraint.activate([title.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 8),
            title.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -8),
            title.topAnchor.constraint(equalTo: cell.topAnchor, constant: rows[row].heading == nil ? 9 : 8)])
        if let report = rows[row].report {
            let time = report["start_time"] as? String ?? "Unknown start"
            let count = report["selected"] as? Int ?? 0
            let quality = Self.qualityLabel(report)
            let detail = NSTextField(labelWithString: "\(time) · \(count) \(count == 1 ? "recording" : "recordings")\n\(quality)")
            detail.font = .systemFont(ofSize: 10); detail.textColor = Self.needsReview(report) ? .systemOrange : .secondaryLabelColor
            detail.maximumNumberOfLines = 2; detail.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(detail)
            NSLayoutConstraint.activate([detail.leadingAnchor.constraint(equalTo: title.leadingAnchor),
                detail.trailingAnchor.constraint(equalTo: title.trailingAnchor), detail.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 4)])
        }
        return cell
    }
    func tableViewSelectionDidChange(_ notification: Notification) { readReport(at: table.selectedRow) }
    func readReport(at index: Int) {
        guard rows.indices.contains(index), let report = rows[index].report, let id = report["id"] as? String else { return }
        readGeneration += 1; let generation = readGeneration
        selectedReport = report; sources = []; sourcePicker.removeAllItems(); updateActions()
        fullMarkdown = ""; readingMarkdown = nil; reportDetails.state = .off; reportDetails.isHidden = true
        titleLabel.stringValue = report["title"] as? String ?? "Transcript report"
        detailLabel.stringValue = "Loading transcript…"; transcript.string = ""
        client.request(["action": "read", "report_id": id, "query": search.stringValue]) { [weak self] result in
            guard let self, generation == self.readGeneration else { return }
            switch result {
            case .failure: self.detailLabel.stringValue = "Could not read this report. Refresh the library to retry."
            case .success(let value):
                let report = value["report"] as? [String: Any] ?? report
                self.selectedReport = report
                self.titleLabel.stringValue = report["title"] as? String ?? "Transcript report"
                let grouping = report["grouping_status"] as? String == "confirmed" ? "Class grouping confirmed" : "Class grouping not confirmed"
                let modified = value["integrity"] as? String == "modified"
                self.detailLabel.stringValue = modified
                    ? "Report edited since export; stored validation no longer applies to this text.\n\(grouping) · Compare changes with the source audio."
                    : "\(Self.qualityLabel(report)) · \(grouping)\nMachine transcript: accuracy still requires listening review."
                self.detailLabel.textColor = modified || Self.needsReview(report) ? .systemOrange : .secondaryLabelColor
                self.sources = value["sources"] as? [[String: Any]] ?? []
                self.sourcePicker.removeAllItems()
                for (i, source) in self.sources.enumerated() {
                    self.sourcePicker.addItem(withTitle: "\(i + 1). \(source["filename"] as? String ?? "Recording")")
                }
                self.fullMarkdown = value["markdown"] as? String ?? "No transcript text is available."
                self.readingMarkdown = ReportPresentation.readingBody(self.fullMarkdown, integrity: value["integrity"] as? String ?? "unknown")
                self.reportDetails.isHidden = self.readingMarkdown == nil
                self.renderSelectedPresentation()
                self.updateActions()
            }
        }
    }
    static func needsReview(_ report: [String: Any]) -> Bool {
        (report["review_required"] as? Int ?? 0) > 0 || (report["failed"] as? Int ?? 0) > 0
            || ["review_required", "failed", "partial"].contains(report["quality_status"] as? String ?? "")
    }
    static func qualityLabel(_ report: [String: Any]) -> String {
        let failed = report["failed"] as? Int ?? 0
        if failed > 0 { return "Partial report · \(failed) unavailable" }
        if needsReview(report) { return "Review needed · provisional transcript" }
        return "Machine transcript · listening review needed"
    }
    @objc func toggleReportDetails() { renderSelectedPresentation() }
    func renderSelectedPresentation() {
        render(reportDetails.state == .on ? fullMarkdown : readingMarkdown ?? fullMarkdown)
    }
    func render(_ markdown: String) {
        // Only style local text: never render embedded remote images, HTML, or active Markdown links.
        let result = NSMutableAttributedString(string: "")
        for line in markdown.components(separatedBy: "\n") {
            var text = line
            var font = NSFont.systemFont(ofSize: 13)
            let style = NSMutableParagraphStyle(); style.lineSpacing = 3; style.paragraphSpacing = 2
            if line.isEmpty {
                font = .systemFont(ofSize: 4); style.lineSpacing = 0; style.paragraphSpacing = 0
                style.minimumLineHeight = 4; style.maximumLineHeight = 4
            }
            else if line.hasPrefix("# ") { text = String(line.dropFirst(2)); font = .systemFont(ofSize: 21, weight: .semibold); style.paragraphSpacing = 8 }
            else if line.hasPrefix("## ") { text = String(line.dropFirst(3)); font = .systemFont(ofSize: 17, weight: .semibold); style.paragraphSpacingBefore = 7 }
            else if line.hasPrefix("### ") { text = String(line.dropFirst(4)); font = .systemFont(ofSize: 15, weight: .semibold); style.paragraphSpacingBefore = 5 }
            else if line.hasPrefix("**"), line.hasSuffix("**"), line.count > 4 {
                text = String(line.dropFirst(2).dropLast(2)); font = .systemFont(ofSize: 13, weight: .semibold)
            }
            else if line.hasPrefix("- ") { text = "• " + line.dropFirst(2) }
            text = text.replacingOccurrences(of: #"\\([\\`*_{}\[\]<>#!|])"#, with: "$1", options: .regularExpression)
            let offset = result.length
            result.append(NSAttributedString(string: text + "\n", attributes: [.font: font, .foregroundColor: NSColor.labelColor, .paragraphStyle: style]))
            if text.hasPrefix("["), let end = text.firstIndex(of: "]"), text.distance(from: text.startIndex, to: end) < 80 {
                let prefix = String(text[...end]) as NSString
                result.addAttributes([.font: NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .medium), .foregroundColor: NSColor.secondaryLabelColor], range: NSRange(location: offset, length: prefix.length))
            }
        }
        if !search.stringValue.isEmpty {
            let text = result.string as NSString; var range = NSRange(location: 0, length: text.length)
            while range.length > 0 {
                let match = text.range(of: search.stringValue, options: [.caseInsensitive, .diacriticInsensitive], range: range)
                if match.location == NSNotFound { break }
                result.addAttribute(.backgroundColor, value: NSColor.systemYellow.withAlphaComponent(0.3), range: match)
                range = NSRange(location: NSMaxRange(match), length: text.length - NSMaxRange(match))
            }
        }
        transcript.textStorage?.setAttributedString(result); transcript.scrollRangeToVisible(NSRange(location: 0, length: 0))
    }
    func clearReader() {
        readGeneration += 1; selectedReport = nil; sources = []; sourcePicker.removeAllItems()
        fullMarkdown = ""; readingMarkdown = nil; reportDetails.isHidden = true; reportDetails.state = .off
        titleLabel.stringValue = "Your transcript library"; detailLabel.stringValue = "Select a report to read it here."
        transcript.string = ""; updateActions()
    }
    func updateActions() {
        let ready = currentPath != nil
        open.isEnabled = ready; reveal.isEnabled = ready; rename.isEnabled = ready
        regroup.isEnabled = canRequeue && !sources.isEmpty && sources.allSatisfy { source in
            guard let path = source["audio_path"] as? String else { return false }
            return FileManager.default.fileExists(atPath: path)
        }
        sourcePicker.isEnabled = !sources.isEmpty
        sourceChanged()
    }
    @objc func sourceChanged() {
        let index = sourcePicker.indexOfSelectedItem
        openAudio.isEnabled = sources.indices.contains(index) && (sources[index]["audio_path"] as? String).map { FileManager.default.fileExists(atPath: $0) } == true
    }
    @objc func openSource() {
        guard sources.indices.contains(sourcePicker.indexOfSelectedItem),
              let path = sources[sourcePicker.indexOfSelectedItem]["audio_path"] as? String else { return }
        if !NSWorkspace.shared.open(URL(fileURLWithPath: path)) { message.stringValue = "Could not open the source audio in its default app." }
    }
    @objc func openReport() {
        if let path = currentPath, !NSWorkspace.shared.open(URL(fileURLWithPath: path)) { message.stringValue = "Could not open the report. Use Show in Finder." }
    }
    @objc func revealReport() { if let path = currentPath { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)]) } }
    @objc func requeueReport() {
        let urls = sources.compactMap { ($0["audio_path"] as? String).map { URL(fileURLWithPath: $0) } }
        guard urls.count == sources.count else { return }; onRequeue?(urls)
    }
    @objc func renameReport() {
        guard let id = selectedReport?["id"] as? String, let window = view.window else { return }
        let alert = NSAlert(); alert.messageText = "Rename report"
        alert.informativeText = "This library title does not change the original recordings or transcript text."
        let field = NSTextField(string: selectedReport?["title"] as? String ?? "Transcript report")
        field.frame = NSRect(x: 0, y: 0, width: 340, height: 26); alert.accessoryView = field
        alert.addButton(withTitle: "Save"); alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self, response == .alertFirstButtonReturn else { return }
            let title = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !title.isEmpty else { return }
            self.client.request(["action": "rename", "report_id": id, "title": title]) { result in
                switch result { case .success: self.reload(); case .failure: self.message.stringValue = "Could not save this title. Please retry." }
            }
        }
    }
}

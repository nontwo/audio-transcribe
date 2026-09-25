import Cocoa

private struct LibraryRow {
    var heading: String?
    var report: [String: Any]?
}

final class LibraryController: NSViewController, NSTableViewDataSource, NSTableViewDelegate, NSSearchFieldDelegate {
    let client = LibraryClient()
    let table = NSTableView()
    let search = NSSearchField()
    let titleLabel = NSTextField(labelWithString: "选择转录结果")
    let detailLabel = NSTextField(wrappingLabelWithString: "转录完成后，正文会显示在这里。")
    let transcript = NSTextView()
    let sourcePicker = NSPopUpButton()
    let openAudio = NSButton(title: "打开音频", target: nil, action: nil)
    let reportDetails = NSButton(checkboxWithTitle: "报告详情", target: nil, action: nil)
    let rename = NSButton(title: "重命名…", target: nil, action: nil)
    let regroup = NSButton(title: "调整课程分组…", target: nil, action: nil)
    let open = NSButton(title: "打开完整报告", target: nil, action: nil)
    let reveal = NSButton(title: "在 Finder 中显示", target: nil, action: nil)
    let remove = NSButton(title: "移到最近删除…", target: nil, action: nil)
    let importRecordings = NSButton(title: "导入录音…", target: nil, action: nil)
    let taskBanner = NSButton(title: "查看转录任务", target: nil, action: nil)
    let viewPicker = NSSegmentedControl(labels: ["最近生成", "按录音日期", "最近删除"], trackingMode: .selectOne, target: nil, action: nil)
    let message = NSTextField(wrappingLabelWithString: "正在加载转录结果…")
    var onRequeue: (([URL]) -> Void)?
    var onImport: (() -> Void)?
    var onShowTasks: (() -> Void)?
    var onSelection: ((String?) -> Void)?
    var onReportTrashed: ((String) -> Void)?
    var taskSummary: String? { didSet { updateTaskBanner() } }
    var canImport = true { didSet { importRecordings.isEnabled = canImport } }
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
    private var displayMode = LibraryDisplayMode.recent
    private var mutating = false
    var currentPath: String? { selectedReport?["report"] as? String }

    override func loadView() {
        view = NSView()
        let root = NSStackView(); root.orientation = .vertical; root.alignment = .leading; root.spacing = 12
        root.translatesAutoresizingMaskIntoConstraints = false; view.addSubview(root)
        NSLayoutConstraint.activate([root.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 18),
            root.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -18),
            root.topAnchor.constraint(equalTo: view.topAnchor, constant: 18),
            root.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -16)])
        let heading = NSTextField(labelWithString: "转录结果"); heading.font = .systemFont(ofSize: 23, weight: .semibold)
        search.placeholderString = "搜索文件名、课程或正文"; search.delegate = self
        search.setAccessibilityLabel("搜索转录结果")
        let reload = NSButton(title: "刷新", target: self, action: #selector(reloadLibrary)); reload.bezelStyle = .rounded
        importRecordings.target = self; importRecordings.action = #selector(importFiles); importRecordings.bezelStyle = .rounded
        importRecordings.font = .systemFont(ofSize: 13, weight: .semibold); importRecordings.isEnabled = canImport
        let toolbar = NSStackView(views: [heading, NSView(), search, reload, importRecordings]); toolbar.spacing = 12
        root.addArrangedSubview(toolbar); toolbar.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        search.widthAnchor.constraint(equalToConstant: 320).isActive = true
        let description = NSTextField(wrappingLabelWithString: "这里显示已生成的转录报告；按生成时间查看，或切换到录音日期。正在处理的录音请到“任务”查看。")
        description.font = .systemFont(ofSize: 12); description.textColor = .secondaryLabelColor
        root.addArrangedSubview(description); description.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        viewPicker.selectedSegment = displayMode.rawValue; viewPicker.target = self; viewPicker.action = #selector(changeView)
        viewPicker.setAccessibilityLabel("结果查看方式")
        taskBanner.target = self; taskBanner.action = #selector(showTasks); taskBanner.bezelStyle = .rounded
        taskBanner.cell?.lineBreakMode = .byTruncatingTail
        taskBanner.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let filterBar = NSStackView(views: [viewPicker, NSView(), taskBanner]); filterBar.spacing = 12
        root.addArrangedSubview(filterBar); filterBar.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        updateTaskBanner()
        let split = NSSplitView(); split.isVertical = true; split.dividerStyle = .thin
        root.addArrangedSubview(split); split.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        split.heightAnchor.constraint(greaterThanOrEqualToConstant: 340).isActive = true
        split.setContentHuggingPriority(.defaultLow, for: .vertical)
        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("report"))
        column.minWidth = 230; column.width = 305; table.addTableColumn(column)
        table.headerView = nil; table.style = .sourceList; table.delegate = self; table.dataSource = self
        table.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle; table.setAccessibilityLabel("转录结果列表")
        let listScroll = NSScrollView(); listScroll.documentView = table; listScroll.hasVerticalScroller = true
        listScroll.translatesAutoresizingMaskIntoConstraints = false
        split.addArrangedSubview(listScroll); listScroll.widthAnchor.constraint(greaterThanOrEqualToConstant: 250).isActive = true
        let reader = NSStackView(); reader.orientation = .vertical; reader.alignment = .leading; reader.spacing = 8
        reader.edgeInsets = NSEdgeInsets(top: 0, left: 16, bottom: 0, right: 0)
        split.addArrangedSubview(reader); reader.widthAnchor.constraint(greaterThanOrEqualToConstant: 470).isActive = true
        titleLabel.font = .systemFont(ofSize: 19, weight: .semibold); titleLabel.lineBreakMode = .byTruncatingTail
        reader.addArrangedSubview(titleLabel); titleLabel.widthAnchor.constraint(equalTo: reader.widthAnchor, constant: -16).isActive = true
        detailLabel.font = .systemFont(ofSize: 12); detailLabel.textColor = .secondaryLabelColor
        reader.addArrangedSubview(detailLabel); detailLabel.widthAnchor.constraint(equalTo: titleLabel.widthAnchor).isActive = true
        sourcePicker.target = self; sourcePicker.action = #selector(sourceChanged)
        sourcePicker.setAccessibilityLabel("源录音")
        openAudio.target = self; openAudio.action = #selector(openSource); openAudio.bezelStyle = .rounded
        reportDetails.target = self; reportDetails.action = #selector(toggleReportDetails)
        reportDetails.toolTip = "显示报告说明和元数据。“打开完整报告”始终打开未删减的文件。"
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
        transcript.setAccessibilityLabel("转录正文")
        textScroll.documentView = transcript; reader.addArrangedSubview(textScroll)
        textScroll.widthAnchor.constraint(equalTo: titleLabel.widthAnchor).isActive = true
        textScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 160).isActive = true
        textScroll.setContentHuggingPriority(.defaultLow, for: .vertical)
        for button in [rename, regroup, open, reveal, remove] { button.target = self; button.bezelStyle = .rounded }
        rename.action = #selector(renameReport); regroup.action = #selector(requeueReport)
        open.action = #selector(openReport); reveal.action = #selector(revealReport)
        remove.action = #selector(trashOrRestoreReport)
        let editActions = NSStackView(views: [rename, regroup, NSView(), remove]); editActions.spacing = 6
        reader.addArrangedSubview(editActions); editActions.widthAnchor.constraint(equalTo: titleLabel.widthAnchor).isActive = true
        let actions = NSStackView(views: [NSView(), open, reveal]); actions.spacing = 6
        reader.addArrangedSubview(actions); actions.widthAnchor.constraint(equalTo: titleLabel.widthAnchor).isActive = true
        message.font = .systemFont(ofSize: 11); message.textColor = .secondaryLabelColor
        root.addArrangedSubview(message); message.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        updateActions()
        DispatchQueue.main.async { split.setPosition(305, ofDividerAt: 0) }
    }

    func reload(selectPath: String? = nil) {
        _ = view
        pendingPath = selectPath ?? currentPath
        listGeneration += 1; let generation = listGeneration
        message.stringValue = "正在加载转录结果…"
        client.request(["action": "list", "query": search.stringValue, "view": displayMode.backendView, "sort": displayMode.backendSort]) { [weak self] result in
            guard let self, generation == self.listGeneration else { return }
            switch result {
            case .failure:
                self.message.stringValue = "无法加载本机转录结果，请点“刷新”重试。"
            case .success(let value):
                self.rows = []
                if self.displayMode.usesDateGroups {
                    for date in value["dates"] as? [[String: Any]] ?? [] {
                        let reports = date["reports"] as? [[String: Any]] ?? []
                        guard !reports.isEmpty else { continue }
                        self.rows.append(LibraryRow(heading: date["date"] as? String ?? "录音日期未知"))
                        self.rows += reports.map { LibraryRow(report: $0) }
                    }
                } else {
                    self.rows = (value["reports"] as? [[String: Any]] ?? []).map { LibraryRow(report: $0) }
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
                let active = value["active_count"] as? Int ?? 0; let trash = value["trash_count"] as? Int ?? 0
                if count == 0 {
                    self.message.stringValue = !self.search.stringValue.isEmpty ? "没有匹配的转录结果。" : self.displayMode == .trash ? "最近删除为空。删除的报告可以在这里恢复。" : "还没有转录结果。点右上角“导入录音…”开始；已导入的录音请到“任务”查看。"
                } else {
                    let explanation = self.displayMode == .trash ? "这里的报告已从结果列表移除，可以恢复；原始录音和转录文件仍保留。" : self.displayMode == .recent ? "按报告生成时间排序，最新结果在上方；录音日期另列。" : "按录音日期和开始时间分类；录音日期与报告生成时间不同。"
                    self.message.stringValue = "\(explanation) 共 \(active) 份结果，最近删除 \(trash) 份。"
                }
            }
        }
    }
    @objc func reloadLibrary() { reload() }
    func showRecentResults(selectPath: String? = nil) {
        _ = view; displayMode = .recent; viewPicker.selectedSegment = displayMode.rawValue
        searchTimer?.invalidate(); search.stringValue = ""; clearReader(); reload(selectPath: selectPath)
    }
    @objc func changeView() {
        displayMode = LibraryDisplayMode(rawValue: viewPicker.selectedSegment) ?? .recent
        searchTimer?.invalidate(); clearReader(); rows = []; table.reloadData(); pendingPath = nil; reload()
    }
    @objc func importFiles() { if canImport { onImport?() } }
    @objc func showTasks() { onShowTasks?() }
    func updateTaskBanner() {
        let summary = taskSummary?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        taskBanner.isHidden = summary.isEmpty
        taskBanner.title = summary + " · 查看任务 →"; taskBanner.toolTip = summary
        taskBanner.setAccessibilityLabel(summary + "，查看转录任务")
    }
    func controlTextDidChange(_ notification: Notification) {
        searchTimer?.invalidate()
        searchTimer = Timer.scheduledTimer(withTimeInterval: 0.3, repeats: false) { [weak self] _ in self?.reload() }
    }
    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }
    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat { rows[row].heading == nil ? 103 : 31 }
    func tableView(_ tableView: NSTableView, shouldSelectRow row: Int) -> Bool { rows[row].report != nil }
    func tableView(_ tableView: NSTableView, isGroupRow row: Int) -> Bool { rows[row].heading != nil }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let cell = NSTableCellView()
        let title = NSTextField(labelWithString: rows[row].heading ?? rows[row].report?["title"] as? String ?? "转录报告")
        title.font = .systemFont(ofSize: 12, weight: .semibold); title.lineBreakMode = .byTruncatingTail
        title.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(title); cell.textField = title
        NSLayoutConstraint.activate([title.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 8),
            title.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -8),
            title.topAnchor.constraint(equalTo: cell.topAnchor, constant: rows[row].heading == nil ? 9 : 8)])
        if let report = rows[row].report {
            let time = report["start_time"] as? String ?? "开始时间未知"
            let count = report["selected"] as? Int ?? 0
            let quality = Self.qualityLabel(report)
            let filenames = report["filenames"] as? [String] ?? []
            let file = NSTextField(labelWithString: LibraryDisplayText.filenames(filenames))
            file.font = .systemFont(ofSize: 10); file.textColor = .secondaryLabelColor; file.lineBreakMode = .byTruncatingMiddle
            file.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(file)
            NSLayoutConstraint.activate([file.leadingAnchor.constraint(equalTo: title.leadingAnchor), file.trailingAnchor.constraint(equalTo: title.trailingAnchor), file.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 3)])
            let multipleDates = (report["dates"] as? [Any] ?? []).count > 1
            let date = report["date"] as? String ?? (multipleDates ? "多个日期" : "日期未知")
            let detail = NSTextField(labelWithString: "\(LibraryDisplayText.generatedTime(report["created_at"] as? String))\n录音 \(date) \(time) · \(count) 段\n\(quality)")
            detail.font = .systemFont(ofSize: 10); detail.textColor = Self.needsReview(report) ? .systemOrange : .secondaryLabelColor
            detail.maximumNumberOfLines = 3; detail.lineBreakMode = .byTruncatingTail; detail.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(detail)
            NSLayoutConstraint.activate([detail.leadingAnchor.constraint(equalTo: title.leadingAnchor),
                detail.trailingAnchor.constraint(equalTo: title.trailingAnchor), detail.topAnchor.constraint(equalTo: file.bottomAnchor, constant: 3)])
            cell.toolTip = ([title.stringValue] + filenames + [detail.stringValue]).joined(separator: "\n")
        }
        return cell
    }
    func tableViewSelectionDidChange(_ notification: Notification) { readReport(at: table.selectedRow) }
    func readReport(at index: Int) {
        guard rows.indices.contains(index), let report = rows[index].report, let id = report["id"] as? String else { return }
        readGeneration += 1; let generation = readGeneration
        selectedReport = report; sources = []; sourcePicker.removeAllItems(); updateActions()
        fullMarkdown = ""; readingMarkdown = nil; reportDetails.state = .off; reportDetails.isHidden = true
        titleLabel.stringValue = report["title"] as? String ?? "转录报告"
        detailLabel.stringValue = "正在加载正文…"; transcript.string = ""
        client.request(["action": "read", "report_id": id, "query": search.stringValue, "view": displayMode.backendView]) { [weak self] result in
            guard let self, generation == self.readGeneration else { return }
            switch result {
            case .failure: self.detailLabel.stringValue = "无法读取这份报告，请刷新结果列表后重试。"
            case .success(let value):
                let report = value["report"] as? [String: Any] ?? report
                self.selectedReport = report
                self.titleLabel.stringValue = report["title"] as? String ?? "转录报告"
                let grouping = report["grouping_status"] as? String == "confirmed" ? "课程分组已确认" : "课程分组尚未确认"
                let modified = value["integrity"] as? String == "modified"
                self.detailLabel.stringValue = modified
                    ? "报告在导出后被修改，原有校验不再适用于当前正文。\n\(grouping) · 请对照源音频检查修改。"
                    : "\(Self.qualityLabel(report)) · \(grouping)\n机器转录，内容准确性仍需听音复核。"
                if self.displayMode == .trash { self.detailLabel.stringValue = "已移到最近删除，可恢复。\n" + self.detailLabel.stringValue }
                self.detailLabel.textColor = modified || Self.needsReview(report) ? .systemOrange : .secondaryLabelColor
                self.sources = value["sources"] as? [[String: Any]] ?? []
                self.sourcePicker.removeAllItems()
                for (i, source) in self.sources.enumerated() {
                    self.sourcePicker.addItem(withTitle: "\(i + 1). \(source["filename"] as? String ?? "录音")")
                }
                self.fullMarkdown = value["markdown"] as? String ?? "没有可显示的转录正文。"
                self.readingMarkdown = ReportPresentation.readingBody(self.fullMarkdown, integrity: value["integrity"] as? String ?? "unknown")
                self.reportDetails.isHidden = self.readingMarkdown == nil
                self.renderSelectedPresentation()
                self.updateActions()
                self.onSelection?(self.currentPath)
            }
        }
    }
    static func needsReview(_ report: [String: Any]) -> Bool {
        (report["review_required"] as? Int ?? 0) > 0 || (report["failed"] as? Int ?? 0) > 0
            || ["review_required", "failed", "partial"].contains(report["quality_status"] as? String ?? "")
    }
    static func qualityLabel(_ report: [String: Any]) -> String {
        let failed = report["failed"] as? Int ?? 0
        if failed > 0 { return "部分完成 · \(failed) 段暂无正文" }
        if needsReview(report) { return "需要复核 · 暂定转录文本" }
        return "机器转录 · 仍需听音复核"
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
        titleLabel.stringValue = "选择转录结果"; detailLabel.stringValue = "从左侧选择一份报告，在这里阅读正文。"
        transcript.string = ""; updateActions()
    }
    func updateActions() {
        let ready = currentPath != nil
        open.isEnabled = ready; reveal.isEnabled = ready; rename.isEnabled = ready && !mutating && displayMode != .trash
        remove.title = displayMode == .trash ? "恢复到转录结果" : "移到最近删除…"
        remove.isEnabled = ready && !mutating
        viewPicker.isEnabled = !mutating
        regroup.isEnabled = canRequeue && !mutating && displayMode != .trash && !sources.isEmpty && sources.allSatisfy { source in
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
        if !NSWorkspace.shared.open(URL(fileURLWithPath: path)) { message.stringValue = "无法用默认应用打开源音频。" }
    }
    @objc func openReport() {
        if let path = currentPath, !NSWorkspace.shared.open(URL(fileURLWithPath: path)) { message.stringValue = "无法打开报告，请使用“在 Finder 中显示”。" }
    }
    @objc func revealReport() { if let path = currentPath { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)]) } }
    @objc func requeueReport() {
        let urls = sources.compactMap { ($0["audio_path"] as? String).map { URL(fileURLWithPath: $0) } }
        guard urls.count == sources.count else { return }; onRequeue?(urls)
    }
    @objc func renameReport() {
        guard let id = selectedReport?["id"] as? String, let window = view.window else { return }
        let alert = NSAlert(); alert.messageText = "重命名转录结果"
        alert.informativeText = "只修改结果列表中的标题，原始录音和转录正文保持不变。"
        let field = NSTextField(string: selectedReport?["title"] as? String ?? "转录报告")
        field.frame = NSRect(x: 0, y: 0, width: 340, height: 26); alert.accessoryView = field
        alert.addButton(withTitle: "保存"); alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self, response == .alertFirstButtonReturn else { return }
            let title = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !title.isEmpty else { return }
            self.client.request(["action": "rename", "report_id": id, "title": title]) { result in
                switch result { case .success: self.reload(); case .failure: self.message.stringValue = "无法保存标题，请重试。" }
            }
        }
    }
    @objc func trashOrRestoreReport() {
        guard !mutating, let id = selectedReport?["id"] as? String, let path = currentPath else { return }
        if displayMode == .trash { setReportDeleted(false, id: id, path: path); return }
        guard let window = view.window else { return }
        let alert = NSAlert(); alert.messageText = "移到最近删除？"
        alert.informativeText = "这份报告将从“转录结果”列表中移除。原始录音和转录文件都会保留；之后可在“最近删除”中恢复。"
        alert.addButton(withTitle: "移到最近删除"); alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { [weak self] response in
            guard response == .alertFirstButtonReturn else { return }
            self?.setReportDeleted(true, id: id, path: path)
        }
    }
    private func setReportDeleted(_ deleted: Bool, id: String, path: String) {
        guard !mutating else { return }
        mutating = true; updateActions()
        message.stringValue = deleted ? "正在移到最近删除…" : "正在恢复转录结果…"
        client.request(["action": deleted ? "trash" : "restore", "report_id": id,
                        "view": displayMode.backendView, "sort": displayMode.backendSort]) { [weak self] result in
            guard let self else { return }
            self.mutating = false; self.updateActions()
            switch result {
            case .failure:
                self.message.stringValue = deleted ? "无法移到最近删除，请重试。" : "无法恢复报告，请重试。"
            case .success:
                if deleted { self.onReportTrashed?(path) }
                self.clearReader(); self.pendingPath = nil
                if deleted { self.reload() }
                else { self.showRecentResults(selectPath: path) }
            }
        }
    }
}

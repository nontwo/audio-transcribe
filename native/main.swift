import Cocoa
import UniformTypeIdentifiers

let reorderType = NSPasteboard.PasteboardType("local.audio-transcribe.queue-row")
let extensions = ["wav", "wave", "m4a", "mp3", "flac", "aac", "aiff", "aif", "aifc", "ogg", "oga", "opus", "mp4", "mov"]

func fileURLs(_ pasteboard: NSPasteboard) -> [URL] {
    (pasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL]) ?? []
}

final class DropTarget: NSView {
    var accept: (([URL]) -> Bool)?
    var enabled = true
    var hovering = false { didSet { needsDisplay = true } }
    override init(frame: NSRect) {
        super.init(frame: frame)
        registerForDraggedTypes([.fileURL])
        setAccessibilityLabel("将录音拖到这里，或点击导入录音")
    }
    required init?(coder: NSCoder) { fatalError() }
    override func draw(_ dirtyRect: NSRect) {
        let rect = bounds.insetBy(dx: 1, dy: 1)
        let shape = NSBezierPath(roundedRect: rect, xRadius: 12, yRadius: 12)
        (hovering ? NSColor.controlAccentColor.withAlphaComponent(0.10) : NSColor.controlBackgroundColor).setFill()
        shape.fill()
        (hovering ? NSColor.controlAccentColor : NSColor.separatorColor).setStroke()
        shape.lineWidth = hovering ? 2 : 1
        shape.setLineDash([6, 4], count: 2, phase: 0)
        shape.stroke()
    }
    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        hovering = enabled && !fileURLs(sender.draggingPasteboard).isEmpty
        return hovering ? .copy : []
    }
    override func draggingExited(_ sender: NSDraggingInfo?) { hovering = false }
    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        hovering = false
        return enabled && (accept?(fileURLs(sender.draggingPasteboard)) ?? false)
    }
}

final class MainController: NSWindowController, NSWindowDelegate, NSTableViewDataSource, NSTableViewDelegate {
    let queue = RecordingQueue()
    let table = NSTableView()
    let drop = DropTarget()
    let count = NSTextField(labelWithString: "尚未导入录音")
    let status = NSTextField(wrappingLabelWithString: "导入录音后会显示在这里。确认顺序，再点击开始转录。")
    let spinner = NSProgressIndicator()
    let choose = NSButton(title: "导入录音…", target: nil, action: nil)
    let clear = NSButton(title: "清空任务列表", target: nil, action: nil)
    let start = NSButton(title: "开始转录", target: nil, action: nil)
    let cancel = NSButton(title: "取消转录", target: nil, action: nil)
    let openReport = NSButton(title: "查看本次结果", target: nil, action: nil)
    let reveal = NSButton(title: "在访达中显示", target: nil, action: nil)
    let tabs = NSTabView()
    let library = LibraryController()
    var groupingSheet: GroupingSheet?
    var planning = false
    var process: Process?
    var reportURL: URL?
    var taskFinished = false
    var stdoutBuffer = Data()
    var receivedTerminalEvent = false
    var cancelRequested = false
    var quitting = false
    var activeIndex: Int?
    var currentProgress: TranscriptionProgress?
    var progressTimer: Timer?
    var running: Bool { process != nil }
    var editingLocked: Bool { running || planning || groupingSheet != nil }
    let defaults = UserDefaults.standard

    init() {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "AudioTranscribe"
        window.minSize = NSSize(width: 980, height: 760)
        window.setFrameAutosaveName("AudioTranscribeMainWindow")
        super.init(window: window)
        window.delegate = self
        buildView()
        if let path = defaults.string(forKey: "LastReport"), FileManager.default.fileExists(atPath: path) {
            reportURL = URL(fileURLWithPath: path)
        }
        if let paths = defaults.stringArray(forKey: "SelectedFiles") {
            var reportEntries: [[String: Any]] = []
            if let reportURL,
               let data = try? Data(contentsOf: reportURL.deletingLastPathComponent().appendingPathComponent("manifest.json")),
               let manifest = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                reportEntries = manifest["ordered_sources"] as? [[String: Any]] ?? []
            }
            let saved = defaults.array(forKey: "QueueState") as? [[String: String]] ?? []
            if QueuePersistence.reportMatches(paths, entries: reportEntries) {
                taskFinished = true
                status.stringValue = "本次任务已有结果，点击查看本次结果。导入新录音会新建一份任务列表。"
            } else {
                reportURL = nil
                taskFinished = QueuePersistence.savedTaskFinished(paths, saved: saved, markedFinished: defaults.bool(forKey: "TaskFinished"))
                if taskFinished { status.stringValue = "上次任务已处理，报告可在「转录结果」或「最近删除」查看。" }
            }
            queue.items = QueuePersistence.restore(paths, saved: saved, reportEntries: taskFinished ? reportEntries : [])
        } else {
            reportURL = nil
        }
        library.onImport = { [weak self] in self?.chooseFiles() }
        library.onShowTasks = { [weak self] in self?.tabs.selectTabViewItem(withIdentifier: "queue") }
        library.onReportTrashed = { [weak self] path in
            guard let self, self.reportURL?.path == path else { return }
            self.reportURL = nil
            self.status.stringValue = "这份报告已移到最近删除，可在「转录结果 → 最近删除」恢复。"
            self.persist(); self.refresh()
        }
        refresh()
        library.onRequeue = { [weak self] urls in
            guard let self, !self.editingLocked else { return }
            self.queue.items = urls.map { Recording(url: $0) }
            self.reportURL = nil; self.taskFinished = false; self.persist(); self.refresh(); self.tabs.selectTabViewItem(withIdentifier: "queue")
            self.transcribe()
        }
        library.showRecentResults(selectPath: reportURL?.path)
        tabs.selectTabViewItem(withIdentifier: !taskFinished && !queue.items.isEmpty ? "queue" : "library")
        window.center()
    }
    required init?(coder: NSCoder) { fatalError() }

    func label(_ text: String, size: CGFloat, weight: NSFont.Weight = .regular) -> NSTextField {
        let v = NSTextField(labelWithString: text)
        v.font = NSFont.systemFont(ofSize: size, weight: weight)
        return v
    }
    func buildView() {
        guard let windowContent = window?.contentView else { return }
        tabs.translatesAutoresizingMaskIntoConstraints = false; windowContent.addSubview(tabs)
        NSLayoutConstraint.activate([tabs.leadingAnchor.constraint(equalTo: windowContent.leadingAnchor, constant: 10),
            tabs.trailingAnchor.constraint(equalTo: windowContent.trailingAnchor, constant: -10),
            tabs.topAnchor.constraint(equalTo: windowContent.topAnchor, constant: 10),
            tabs.bottomAnchor.constraint(equalTo: windowContent.bottomAnchor, constant: -10)])
        let libraryTab = NSTabViewItem(identifier: "library"); libraryTab.label = "转录结果"; libraryTab.view = library.view
        let queueTab = NSTabViewItem(identifier: "queue"); queueTab.label = "任务"
        let content = NSView(); queueTab.view = content
        tabs.addTabViewItem(queueTab); tabs.addTabViewItem(libraryTab)
        let root = NSStackView()
        root.orientation = .vertical; root.alignment = .leading; root.spacing = 16
        root.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 24),
            root.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -24),
            root.topAnchor.constraint(equalTo: content.topAnchor, constant: 24),
            root.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -20)])
        root.addArrangedSubview(label("任务", size: 24, weight: .semibold))
        let subtitle = label("新导入、等待和正在处理的录音都在这里。完成后到「转录结果」查看正文。", size: 13)
        subtitle.textColor = .secondaryLabelColor; root.addArrangedSubview(subtitle)
        drop.translatesAutoresizingMaskIntoConstraints = false
        root.addArrangedSubview(drop)
        drop.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        drop.heightAnchor.constraint(equalToConstant: 112).isActive = true
        drop.accept = { [weak self] urls in self?.add(urls) ?? false }
        let dropContents = NSStackView()
        dropContents.orientation = .vertical; dropContents.spacing = 10
        dropContents.translatesAutoresizingMaskIntoConstraints = false
        drop.addSubview(dropContents)
        let icon = NSImageView(image: NSImage(systemSymbolName: "waveform", accessibilityDescription: "Audio files")!)
        icon.contentTintColor = .controlAccentColor
        dropContents.addArrangedSubview(icon)
        dropContents.addArrangedSubview(label("将录音拖到这里，或点击导入录音", size: 16, weight: .medium))
        choose.target = self; choose.action = #selector(chooseFiles); choose.bezelStyle = .rounded
        dropContents.addArrangedSubview(choose)
        NSLayoutConstraint.activate([dropContents.centerXAnchor.constraint(equalTo: drop.centerXAnchor),
            dropContents.centerYAnchor.constraint(equalTo: drop.centerYAnchor)])
        let listBar = NSStackView(views: [count, NSView(), clear])
        listBar.orientation = .horizontal; listBar.distribution = .fill
        clear.target = self; clear.action = #selector(clearFiles); clear.bezelStyle = .rounded
        count.font = NSFont.systemFont(ofSize: 12); count.textColor = .secondaryLabelColor
        root.addArrangedSubview(listBar); listBar.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        let removeHint = label("移除或清空任务只影响此列表；原录音和已生成的转录结果会保留。", size: 11)
        removeHint.textColor = .secondaryLabelColor; root.addArrangedSubview(removeHint)
        for (identifier, width) in [("number", 38.0), ("name", 432.0), ("state", 130.0), ("remove", 34.0)] {
            let col = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(identifier))
            col.width = width; col.minWidth = identifier == "name" ? 280 : width
            col.resizingMask = identifier == "name" ? .autoresizingMask : []
            table.addTableColumn(col)
        }
        table.headerView = nil; table.rowHeight = 49
        table.style = .fullWidth; table.usesAlternatingRowBackgroundColors = true
        table.allowsMultipleSelection = true; table.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        table.dataSource = self; table.delegate = self
        table.registerForDraggedTypes([reorderType, .fileURL])
        table.setDraggingSourceOperationMask(.move, forLocal: true)
        table.setAccessibilityLabel("当前任务中的录音")
        let scroll = NSScrollView()
        scroll.documentView = table; scroll.hasVerticalScroller = true; scroll.borderType = .bezelBorder
        root.addArrangedSubview(scroll)
        scroll.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 150).isActive = true
        scroll.setContentHuggingPriority(.defaultLow, for: .vertical)
        let progressRow = NSStackView(views: [spinner, status])
        progressRow.alignment = .centerY; progressRow.spacing = 10
        spinner.style = .spinning; spinner.controlSize = .small; spinner.isDisplayedWhenStopped = false
        status.font = NSFont.systemFont(ofSize: 12); status.textColor = .secondaryLabelColor
        status.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        root.addArrangedSubview(progressRow); progressRow.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        for button in [start, cancel, openReport, reveal] { button.target = self; button.bezelStyle = .rounded }
        start.action = #selector(transcribe); start.keyEquivalent = "\r"
        cancel.action = #selector(cancelWork); openReport.action = #selector(openResult); reveal.action = #selector(showResult)
        let actions = NSStackView(views: [openReport, reveal, NSView(), cancel, start])
        actions.spacing = 8; root.addArrangedSubview(actions)
        actions.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
    }
    func persist() {
        defaults.set(queue.items.map { $0.url.path }, forKey: "SelectedFiles")
        defaults.set(QueuePersistence.snapshot(queue.items), forKey: "QueueState")
        defaults.set(taskFinished, forKey: "TaskFinished")
        defaults.set(reportURL?.path, forKey: "LastReport")
    }
    func refresh() {
        choose.isEnabled = !editingLocked; clear.isEnabled = !editingLocked && !queue.items.isEmpty
        start.isEnabled = !editingLocked && !queue.items.isEmpty
        cancel.isHidden = !running; cancel.isEnabled = !cancelRequested
        openReport.isHidden = reportURL == nil; reveal.isHidden = reportURL == nil
        drop.enabled = !editingLocked
        library.canRequeue = !editingLocked
        library.canImport = !editingLocked
        tabs.tabViewItem(at: 0).label = queue.items.isEmpty ? "任务" : "任务 · \(queue.items.count)"
        library.taskSummary = queue.items.isEmpty ? nil : running
            ? "正在处理 \(queue.items.count) 个录音"
            : !taskFinished ? "任务中有 \(queue.items.count) 个录音等待处理"
            : "最近一次任务：\(queue.items.count) 个录音已处理"
        count.stringValue = queue.items.isEmpty ? "尚未导入录音" : "本次任务：\(queue.items.count) 个录音 · 可拖动调整顺序"
        start.title = taskFinished ? "再次处理本次任务" : "开始转录"
        table.reloadData()
    }
    @discardableResult func add(_ urls: [URL]) -> Bool {
        guard !editingLocked, !urls.isEmpty else { return false }
        // A completed batch stays in Results; a new import starts a fresh queue.
        if taskFinished { queue.items.removeAll() }
        queue.add(urls); reportURL = nil; taskFinished = false
        tabs.selectTabViewItem(withIdentifier: "queue")
        status.stringValue = "录音已加入任务。确认顺序后点击开始转录；完成后会自动显示正文。"
        persist(); refresh(); window?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        return true
    }
    @objc func chooseFiles() {
        guard !editingLocked, let window else { return }
        let panel = NSOpenPanel()
        panel.title = "导入录音"; panel.prompt = "添加到任务"
        panel.canChooseDirectories = false; panel.canChooseFiles = true; panel.allowsMultipleSelection = true
        panel.allowedContentTypes = extensions.compactMap { UTType(filenameExtension: $0) }
        panel.beginSheetModal(for: window) { [weak self] answer in
            if answer == .OK { self?.add(panel.urls) }
        }
    }
    @objc func clearFiles() {
        guard !editingLocked else { return }
        queue.items.removeAll(); reportURL = nil; taskFinished = false
        status.stringValue = "导入录音后会显示在这里。确认顺序，再点击开始转录。"
        persist(); refresh()
    }
    @objc func removeFile(_ button: NSButton) {
        guard !editingLocked, queue.items.indices.contains(button.tag) else { return }
        queue.items.remove(at: button.tag); reportURL = nil; taskFinished = false
        status.stringValue = queue.items.isEmpty ? "导入录音，创建新任务。" : "录音已加入任务。确认顺序后点击开始转录；完成后会自动显示正文。"
        persist(); refresh()
    }
    func numberOfRows(in tableView: NSTableView) -> Int { queue.items.count }
    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat {
        queue.items[row].message == nil ? 49 : 79
    }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let item = queue.items[row]
        let cell = NSTableCellView()
        let key = tableColumn?.identifier.rawValue ?? ""
        if key == "remove" {
            let b = NSButton(image: NSImage(systemSymbolName: "xmark.circle", accessibilityDescription: "从任务移除 \(item.url.lastPathComponent)（保留原文件）")!, target: self, action: #selector(removeFile(_:)))
            b.isBordered = false; b.tag = row; b.isEnabled = !editingLocked
            b.frame = NSRect(x: 2, y: 13, width: 24, height: 24); cell.addSubview(b); return cell
        }
        let state = ["waiting":"等待开始", "checking":"正在检查…", "processing":"正在转录…", "completed":"已完成", "review_required":"已生成 · 需复核", "failed":"失败 · 可重试", "cancelled":"已取消"][item.state] ?? item.state
        let text = key == "number" ? "\(row + 1)." : key == "name" ? item.url.lastPathComponent : state
        let field = label(text, size: 13, weight: key == "name" ? .medium : .regular)
        field.lineBreakMode = .byTruncatingMiddle; field.translatesAutoresizingMaskIntoConstraints = false
        cell.addSubview(field); cell.textField = field
        NSLayoutConstraint.activate([field.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 6),
            field.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
            field.topAnchor.constraint(equalTo: cell.topAnchor, constant: 15)])
        if key == "state" { field.textColor = item.state == "failed" ? .systemRed : item.state == "review_required" ? .systemOrange : item.state == "completed" ? .systemGreen : .secondaryLabelColor }
        if key == "name" {
            cell.toolTip = item.url.path
            if let message = item.message {
                let error = NSTextField(wrappingLabelWithString: message)
                error.font = NSFont.systemFont(ofSize: 11); error.textColor = item.state == "review_required" ? .systemOrange : .systemRed
                error.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(error); cell.toolTip = message
                NSLayoutConstraint.activate([error.leadingAnchor.constraint(equalTo: field.leadingAnchor),
                    error.trailingAnchor.constraint(equalTo: field.trailingAnchor), error.topAnchor.constraint(equalTo: field.bottomAnchor, constant: 3)])
            }
        }
        return cell
    }
    func tableView(_ tableView: NSTableView, pasteboardWriterForRow row: Int) -> NSPasteboardWriting? {
        guard !editingLocked else { return nil }
        let item = NSPasteboardItem(); item.setString(queue.items[row].id.uuidString, forType: reorderType); return item
    }
    func tableView(_ tableView: NSTableView, validateDrop info: NSDraggingInfo, proposedRow row: Int, proposedDropOperation op: NSTableView.DropOperation) -> NSDragOperation {
        guard !editingLocked else { return [] }
        tableView.setDropRow(row, dropOperation: .above)
        if info.draggingSource as? NSTableView === table { return .move }
        return fileURLs(info.draggingPasteboard).isEmpty ? [] : .copy
    }
    func tableView(_ tableView: NSTableView, acceptDrop info: NSDraggingInfo, row: Int, dropOperation: NSTableView.DropOperation) -> Bool {
        guard !editingLocked else { return false }
        if info.draggingSource as? NSTableView === table {
            let ids = Set((info.draggingPasteboard.pasteboardItems ?? []).compactMap { $0.string(forType: reorderType) })
            let positions = IndexSet(queue.items.indices.filter { ids.contains(queue.items[$0].id.uuidString) })
            queue.move(positions, to: row); reportURL = nil; taskFinished = false
            status.stringValue = "录音已加入任务。确认顺序后点击开始转录；完成后会自动显示正文。"
            persist(); refresh(); return !positions.isEmpty
        }
        return add(fileURLs(info.draggingPasteboard))
    }
    @objc func transcribe() {
        guard !editingLocked, !queue.items.isEmpty else { return }
        planning = true; status.stringValue = "正在检查录音日期与课程分组…"
        spinner.startAnimation(nil); refresh()
        let files = queue.items.map { $0.url.path }
        library.client.request(["action": "plan", "files": files]) { [weak self] result in
            guard let self else { return }
            self.planning = false; self.spinner.stopAnimation(nil)
            guard self.queue.items.map({ $0.url.path }) == files, let window = self.window else { self.refresh(); return }
            switch result {
            case .failure:
                self.status.stringValue = "未能读取录音日期，请重试。"
            case .success(let plan):
                let sheet = GroupingSheet(plan: plan); self.groupingSheet = sheet
                sheet.completion = { [weak self] groups in
                    guard let self else { return }
                    self.groupingSheet = nil; self.refresh()
                    if let groups { self.beginTranscription(groups: groups) }
                    else { self.status.stringValue = "已返回任务列表，可以继续添加或移除录音。" }
                }
                if let sheetWindow = sheet.window { window.beginSheet(sheetWindow) }
            }
            self.refresh()
        }
    }
    func beginTranscription(groups: [[String: Any]]) {
        guard !editingLocked, !queue.items.isEmpty else { return }
        do {
            guard let codeRoot = Bundle.main.object(forInfoDictionaryKey: "AudioTranscribeCodeRoot") as? String else { throw CocoaError(.fileNoSuchFile) }
            let base = URL(fileURLWithPath: codeRoot)
            let job = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/AudioTranscribe/app/job-" + UUID().uuidString)
            try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            let request = job.appendingPathComponent("request.json")
            try JSONSerialization.data(withJSONObject: ["files":queue.items.map { $0.url.path },
                "groups": groups,
                "retry_failed":queue.items.contains { ["failed", "review_required"].contains($0.state) }]).write(to: request, options: .atomic)
            let stderr = job.appendingPathComponent("backend.log")
            FileManager.default.createFile(atPath: stderr.path, contents: nil, attributes: [.posixPermissions: 0o600])
            let errorHandle = try FileHandle(forWritingTo: stderr)
            let child = Process(); let pipe = Pipe()
            child.executableURL = base.appendingPathComponent(".venv/bin/python")
            child.arguments = ["-m", "audio_transcribe", "app-report", "--request", request.path]
            child.currentDirectoryURL = base
            child.standardInput = FileHandle.nullDevice; child.standardOutput = pipe; child.standardError = errorHandle
            stdoutBuffer.removeAll(); receivedTerminalEvent = false; cancelRequested = false; reportURL = nil; taskFinished = false; activeIndex = nil
            currentProgress = nil
            for i in queue.items.indices { queue.items[i].state = "waiting"; queue.items[i].message = nil }
            try child.run(); process = child
            persist()
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                // One reader preserves JSON-line order and split UTF-8 bytes.
                // EOF is consumed before the final UI transition.
                while true {
                    let data = pipe.fileHandleForReading.availableData
                    if data.isEmpty { break }
                    DispatchQueue.main.async { self?.consume(data) }
                }
                child.waitUntilExit()
                try? errorHandle.close()
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.process = nil; self.spinner.stopAnimation(nil)
                    self.progressTimer?.invalidate(); self.progressTimer = nil
                    if !self.receivedTerminalEvent {
                        self.status.stringValue = self.cancelRequested ? "已取消，完成的文件已保留。点击开始转录可继续。" : "处理已停止，完成的文件已保留，可以重试。"
                        if let i = self.activeIndex, ["waiting", "checking", "processing"].contains(self.queue.items[i].state) {
                            self.queue.items[i].state = self.cancelRequested ? "cancelled" : "failed"
                        }
                    }
                    self.persist(); self.refresh()
                    self.library.updateActions()
                    if self.quitting { NSApp.reply(toApplicationShouldTerminate: true) }
                }
            }
            progressTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.refreshProgress() }
            status.stringValue = "正在准备录音…"; spinner.startAnimation(nil); refresh()
        } catch {
            process = nil; status.stringValue = "未能启动转录，请检查本地运行环境后重试。"
            refresh()
        }
    }
    func consume(_ data: Data) {
        stdoutBuffer.append(data)
        while let newline = stdoutBuffer.firstIndex(of: 10) {
            let line = stdoutBuffer.subdata(in: 0..<newline)
            stdoutBuffer.removeSubrange(0...newline)
            guard let value = try? JSONSerialization.jsonObject(with: line) as? [String:Any], let type = value["type"] as? String else { continue }
            if type == "file", let i = value["index"] as? Int, queue.items.indices.contains(i), let state = value["state"] as? String {
                if activeIndex != i || state == "checking" {
                    currentProgress = TranscriptionProgress(started: ProcessInfo.processInfo.systemUptime)
                }
                activeIndex = i; queue.items[i].state = state; queue.items[i].message = value["message"] as? String
                if !cancelRequested {
                    status.stringValue = "正在处理第 \(i+1) / \(queue.items.count) 个文件"
                    refreshProgress()
                }
                persist()
                table.noteHeightOfRows(withIndexesChanged: IndexSet(integer: i)); table.reloadData()
            } else if type == "progress", let i = value["index"] as? Int, i == activeIndex,
                      !receivedTerminalEvent, !cancelRequested {
                currentProgress?.update(value)
                refreshProgress()
            } else if type == "result", let path = value["report"] as? String {
                receivedTerminalEvent = true; reportURL = URL(fileURLWithPath: path); taskFinished = true
                let done = value["completed"] as? Int ?? 0; let failed = value["failed"] as? Int ?? 0
                let review = value["review_required"] as? Int ?? 0
                status.stringValue = failed > 0 || review > 0 ? "结果已生成：\(done) 个完成，\(review) 个需复核，\(failed) 个失败。点击查看本次结果。" : "\(done) 个录音已处理完成。点击查看本次结果，可阅读全文并核对音频。"
                persist(); refresh()
                library.showRecentResults(selectPath: path); tabs.selectTabViewItem(withIdentifier: "library")
            } else if type == "cancelled" || type == "error" {
                receivedTerminalEvent = true
                status.stringValue = value["message"] as? String ?? "处理已停止，请重试。"
                if let i = activeIndex, ["waiting", "checking", "processing"].contains(queue.items[i].state) {
                    queue.items[i].state = type == "cancelled" ? "cancelled" : "failed"
                }
            }
        }
    }
    func refreshProgress() {
        guard running, !cancelRequested, !receivedTerminalEvent, let i = activeIndex,
              queue.items.indices.contains(i), ["checking", "processing"].contains(queue.items[i].state),
              let progress = currentProgress else { return }
        status.stringValue = progress.status(index: i, total: queue.items.count, now: ProcessInfo.processInfo.systemUptime)
    }
    @objc func cancelWork() {
        guard let process, !cancelRequested else { return }
        cancelRequested = true; status.stringValue = "正在取消，已完成的结果会保留…"; cancel.isEnabled = false; process.terminate()
    }
    @objc func openResult() {
        if let reportURL {
            library.showRecentResults(selectPath: reportURL.path)
            tabs.selectTabViewItem(withIdentifier: "library")
        }
    }
    @objc func showResult() { if let reportURL { NSWorkspace.shared.activateFileViewerSelecting([reportURL]) } }
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if running { status.stringValue = "请先取消当前转录，再关闭窗口。"; return false }
        return true
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    lazy var controller = MainController()
    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu(); let appItem = NSMenuItem(); let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About AudioTranscribe", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator()); appMenu.addItem(withTitle: "Quit AudioTranscribe", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu; menu.addItem(appItem)
        let fileItem = NSMenuItem(); let fileMenu = NSMenu(title: "File")
        let chooseItem = NSMenuItem(title: "导入录音…", action: #selector(MainController.chooseFiles), keyEquivalent: "o")
        chooseItem.target = controller; fileMenu.addItem(chooseItem); fileItem.submenu = fileMenu; menu.addItem(fileItem)
        let editItem = NSMenuItem(); let editMenu = NSMenu(title: "Edit")
        for (name, action, key) in [("Cut", "cut:", "x"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")] {
            editMenu.addItem(withTitle: name, action: Selector(action), keyEquivalent: key)
        }
        let find = NSMenuItem(title: "在转录正文中查找", action: #selector(NSTextView.performFindPanelAction(_:)), keyEquivalent: "f")
        find.tag = Int(NSFindPanelAction.showFindPanel.rawValue); find.target = controller.library.transcript
        editMenu.addItem(.separator()); editMenu.addItem(find)
        editItem.submenu = editMenu; menu.addItem(editItem); NSApp.mainMenu = menu
        controller.showWindow(nil); NSApp.activate(ignoringOtherApps: true)
    }
    func application(_ sender: NSApplication, openFiles filenames: [String]) {
        let accepted = controller.add(filenames.map { URL(fileURLWithPath: $0) })
        sender.reply(toOpenOrPrint: accepted ? .success : .failure)
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        controller.showWindow(nil); return true
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if controller.running { controller.quitting = true; controller.cancelWork(); return .terminateLater }
        return .terminateNow
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate(); app.delegate = delegate
app.run()

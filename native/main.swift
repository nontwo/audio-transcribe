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
        setAccessibilityLabel("Drop recordings here")
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
    let count = NSTextField(labelWithString: "No recordings selected")
    let status = NSTextField(wrappingLabelWithString: "Add recordings, then drag rows into the desired order.")
    let spinner = NSProgressIndicator()
    let choose = NSButton(title: "Choose Files…", target: nil, action: nil)
    let clear = NSButton(title: "Clear List", target: nil, action: nil)
    let start = NSButton(title: "Transcribe", target: nil, action: nil)
    let cancel = NSButton(title: "Cancel", target: nil, action: nil)
    let openReport = NSButton(title: "Open Report", target: nil, action: nil)
    let reveal = NSButton(title: "Show in Finder", target: nil, action: nil)
    let tabs = NSTabView()
    let library = LibraryController()
    var groupingSheet: GroupingSheet?
    var planning = false
    var process: Process?
    var reportURL: URL?
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
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 770),
            styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "AudioTranscribe"
        window.minSize = NSSize(width: 980, height: 720)
        window.setFrameAutosaveName("AudioTranscribeMainWindow")
        super.init(window: window)
        window.delegate = self
        buildView()
        if let paths = defaults.stringArray(forKey: "SelectedFiles") {
            queue.items = paths.map { Recording(url: URL(fileURLWithPath: $0)) }
        }
        if let path = defaults.string(forKey: "LastReport"), FileManager.default.fileExists(atPath: path) {
            reportURL = URL(fileURLWithPath: path)
            status.stringValue = "Your last report is ready. Add recordings to start a new report."
        }
        refresh()
        library.onRequeue = { [weak self] urls in
            guard let self, !self.editingLocked else { return }
            self.queue.items = urls.map { Recording(url: $0) }
            self.reportURL = nil; self.persist(); self.refresh(); self.tabs.selectTabViewItem(at: 1)
            self.transcribe()
        }
        library.reload(selectPath: reportURL?.path)
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
        let libraryTab = NSTabViewItem(identifier: "library"); libraryTab.label = "Library"; libraryTab.view = library.view
        let queueTab = NSTabViewItem(identifier: "queue"); queueTab.label = "New Transcription"
        let content = NSView(); queueTab.view = content
        tabs.addTabViewItem(libraryTab); tabs.addTabViewItem(queueTab)
        let root = NSStackView()
        root.orientation = .vertical; root.alignment = .leading; root.spacing = 16
        root.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 24),
            root.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -24),
            root.topAnchor.constraint(equalTo: content.topAnchor, constant: 24),
            root.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -20)])
        root.addArrangedSubview(label("AudioTranscribe", size: 24, weight: .semibold))
        let subtitle = label("Add recordings, review class groups, then read your complete transcript in the Library.", size: 13)
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
        dropContents.addArrangedSubview(label("Drop recordings here", size: 16, weight: .medium))
        choose.target = self; choose.action = #selector(chooseFiles); choose.bezelStyle = .rounded
        dropContents.addArrangedSubview(choose)
        NSLayoutConstraint.activate([dropContents.centerXAnchor.constraint(equalTo: drop.centerXAnchor),
            dropContents.centerYAnchor.constraint(equalTo: drop.centerYAnchor)])
        let listBar = NSStackView(views: [count, NSView(), clear])
        listBar.orientation = .horizontal; listBar.distribution = .fill
        clear.target = self; clear.action = #selector(clearFiles); clear.bezelStyle = .rounded
        count.font = NSFont.systemFont(ofSize: 12); count.textColor = .secondaryLabelColor
        root.addArrangedSubview(listBar); listBar.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
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
        table.setAccessibilityLabel("Ordered recordings")
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
        defaults.set(reportURL?.path, forKey: "LastReport")
    }
    func refresh() {
        choose.isEnabled = !editingLocked; clear.isEnabled = !editingLocked && !queue.items.isEmpty
        start.isEnabled = !editingLocked && !queue.items.isEmpty
        cancel.isHidden = !running; cancel.isEnabled = !cancelRequested
        openReport.isHidden = reportURL == nil; reveal.isHidden = reportURL == nil
        drop.enabled = !editingLocked
        library.canRequeue = !editingLocked
        count.stringValue = queue.items.isEmpty ? "No recordings selected" : "\(queue.items.count) \(queue.items.count == 1 ? "recording" : "recordings") · drag rows to reorder"
        table.reloadData()
    }
    @discardableResult func add(_ urls: [URL]) -> Bool {
        guard !editingLocked, !urls.isEmpty else { return false }
        queue.add(urls); reportURL = nil
        tabs.selectTabViewItem(at: 1)
        status.stringValue = "Confirm the order, then click Transcribe."
        persist(); refresh(); window?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        return true
    }
    @objc func chooseFiles() {
        guard !editingLocked, let window else { return }
        let panel = NSOpenPanel()
        panel.title = "Choose Recordings"; panel.prompt = "Add Recordings"
        panel.canChooseDirectories = false; panel.canChooseFiles = true; panel.allowsMultipleSelection = true
        panel.allowedContentTypes = extensions.compactMap { UTType(filenameExtension: $0) }
        panel.beginSheetModal(for: window) { [weak self] answer in
            if answer == .OK { self?.add(panel.urls) }
        }
    }
    @objc func clearFiles() {
        guard !editingLocked else { return }
        queue.items.removeAll(); reportURL = nil
        status.stringValue = "Add recordings, then drag rows into the desired order."
        persist(); refresh()
    }
    @objc func removeFile(_ button: NSButton) {
        guard !editingLocked, queue.items.indices.contains(button.tag) else { return }
        queue.items.remove(at: button.tag); reportURL = nil
        status.stringValue = queue.items.isEmpty ? "Add recordings to begin." : "Confirm the order, then click Transcribe."
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
            let b = NSButton(image: NSImage(systemSymbolName: "xmark.circle", accessibilityDescription: "Remove \(item.url.lastPathComponent)")!, target: self, action: #selector(removeFile(_:)))
            b.isBordered = false; b.tag = row; b.isEnabled = !editingLocked
            b.frame = NSRect(x: 2, y: 13, width: 24, height: 24); cell.addSubview(b); return cell
        }
        let state = ["waiting":"Waiting", "checking":"Checking…", "processing":"Transcribing…", "completed":"Checks passed", "review_required":"Review needed", "failed":"Failed", "cancelled":"Cancelled"][item.state] ?? item.state
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
            queue.move(positions, to: row); reportURL = nil
            status.stringValue = "Confirm the order, then click Transcribe."
            persist(); refresh(); return !positions.isEmpty
        }
        return add(fileURLs(info.draggingPasteboard))
    }
    @objc func transcribe() {
        guard !editingLocked, !queue.items.isEmpty else { return }
        planning = true; status.stringValue = "Checking recording dates and class continuity…"
        spinner.startAnimation(nil); refresh()
        let files = queue.items.map { $0.url.path }
        library.client.request(["action": "plan", "files": files]) { [weak self] result in
            guard let self else { return }
            self.planning = false; self.spinner.stopAnimation(nil)
            guard self.queue.items.map({ $0.url.path }) == files, let window = self.window else { self.refresh(); return }
            switch result {
            case .failure:
                self.status.stringValue = "Could not inspect recording dates. Please try Transcribe again."
            case .success(let plan):
                let sheet = GroupingSheet(plan: plan); self.groupingSheet = sheet
                sheet.completion = { [weak self] groups in
                    guard let self else { return }
                    self.groupingSheet = nil; self.refresh()
                    if let groups { self.beginTranscription(groups: groups) }
                    else { self.status.stringValue = "Review cancelled. Your recording order is unchanged." }
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
            stdoutBuffer.removeAll(); receivedTerminalEvent = false; cancelRequested = false; reportURL = nil; activeIndex = nil
            currentProgress = nil
            for i in queue.items.indices { queue.items[i].state = "waiting"; queue.items[i].message = nil }
            try child.run(); process = child
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
                        self.status.stringValue = self.cancelRequested ? "Cancelled. Completed files are preserved. Click Transcribe to resume." : "Processing stopped. Completed files are preserved. Please retry."
                        if let i = self.activeIndex { self.queue.items[i].state = self.cancelRequested ? "cancelled" : "failed" }
                    }
                    self.persist(); self.refresh()
                    self.library.updateActions()
                    if self.quitting { NSApp.reply(toApplicationShouldTerminate: true) }
                }
            }
            progressTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.refreshProgress() }
            status.stringValue = "Preparing recordings…"; spinner.startAnimation(nil); refresh()
        } catch {
            process = nil; status.stringValue = "AudioTranscribe could not start. Rebuild the local app, then try again."
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
                    status.stringValue = "Processing \(i+1) of \(queue.items.count)"
                    refreshProgress()
                }
                table.noteHeightOfRows(withIndexesChanged: IndexSet(integer: i)); table.reloadData()
            } else if type == "progress", let i = value["index"] as? Int, i == activeIndex,
                      !receivedTerminalEvent, !cancelRequested {
                currentProgress?.update(value)
                refreshProgress()
            } else if type == "result", let path = value["report"] as? String {
                receivedTerminalEvent = true; reportURL = URL(fileURLWithPath: path)
                let done = value["completed"] as? Int ?? 0; let failed = value["failed"] as? Int ?? 0
                let review = value["review_required"] as? Int ?? 0
                status.stringValue = failed > 0 || review > 0 ? "Report ready: \(done) checks passed, \(review) need review, \(failed) unavailable. Read the quality notes in Library." : "Report ready · \(done) recordings processed. Listening review is still needed."
                persist(); refresh()
                library.reload(selectPath: path); tabs.selectTabViewItem(at: 0)
            } else if type == "cancelled" || type == "error" {
                receivedTerminalEvent = true
                status.stringValue = value["message"] as? String ?? "Processing stopped. Please retry."
                if let i = activeIndex, queue.items[i].state != "completed" { queue.items[i].state = type == "cancelled" ? "cancelled" : "failed" }
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
        cancelRequested = true; status.stringValue = "Cancelling safely…"; cancel.isEnabled = false; process.terminate()
    }
    @objc func openResult() {
        if let reportURL {
            status.stringValue = NSWorkspace.shared.open(reportURL) ? "Report opened in your Markdown app." : "Could not open the report. Use Show in Finder to locate it."
        }
    }
    @objc func showResult() { if let reportURL { NSWorkspace.shared.activateFileViewerSelecting([reportURL]) } }
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if running { status.stringValue = "Cancel the current transcription before closing."; return false }
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
        let chooseItem = NSMenuItem(title: "Choose Files…", action: #selector(MainController.chooseFiles), keyEquivalent: "o")
        chooseItem.target = controller; fileMenu.addItem(chooseItem); fileItem.submenu = fileMenu; menu.addItem(fileItem)
        let editItem = NSMenuItem(); let editMenu = NSMenu(title: "Edit")
        for (name, action, key) in [("Cut", "cut:", "x"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")] {
            editMenu.addItem(withTitle: name, action: Selector(action), keyEquivalent: key)
        }
        let find = NSMenuItem(title: "Find in Transcript", action: #selector(NSTextView.performFindPanelAction(_:)), keyEquivalent: "f")
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

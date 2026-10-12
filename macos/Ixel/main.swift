// Ixel on a Mac: a native window around the browser app that `ixel app --host` serves.
//
// install.sh builds this (with swiftc, from the Command Line Tools) into ~/Applications/Ixel.app and
// writes the install's Python into Info.plist (IxelPython). At launch it starts that Python through your
// login shell, so the model CLIs you installed with Homebrew or npm are on its PATH as they are in
// Terminal (an app opened from the Dock otherwise gets only /usr/bin:/bin:/usr/sbin:/sbin). The page's
// address, with this session's key, comes back on a pipe only this app reads. Quitting closes the
// server's stdin, which stops it (and any review it's running).
// Downloads go to your Downloads folder, and links away from Ixel open in your browser.

import AppKit
import WebKit

let hostPrefix = "IXEL-URL "

// Not marked @MainActor: AppKit calls it on the main thread, and the classic main.swift setup below compiles
// on every Swift 5 toolchain that way.
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var server: Process?
    private var serverInput: Pipe?
    private var quitting = false
    private var address: URL?  // the page's, once the server has printed it
    private var downloads: [WKDownload: URL] = [:]  // where each running download is being saved

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = makeMenu()
        follow(savedAppearance())  // before the window shows, so it opens in your colors
        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(self, name: "ixelAppearance")
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.uiDelegate = self
        webView.navigationDelegate = self
        let visible = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1280, height: 840)
        let size = NSSize(width: min(1280, visible.width * 0.9), height: min(840, visible.height * 0.9))
        window = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Ixel"
        window.minSize = NSSize(width: 480, height: 420)
        window.contentView = webView
        window.center()
        window.setFrameAutosaveName("Ixel")  // the next launch opens where you left it
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        startServer()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopServer()
    }

    // ── The server ──────────────────────────────────────────────────────────

    private func startServer() {
        guard let python = Bundle.main.object(forInfoDictionaryKey: "IxelPython") as? String,
              FileManager.default.isExecutableFile(atPath: python) else {
            fail("Ixel's Python isn't where it was installed. Run Ixel's install.sh again.")
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: loginShell())
        process.arguments = ["-l", "-c", "exec \"$0\" -I -m ixel_mat app --host", python]
        var environment = ProcessInfo.processInfo.environment
        if let extra = Bundle.main.object(forInfoDictionaryKey: "IxelPath") as? String, !extra.isEmpty {
            // The PATH install.sh ran with, in case your profile doesn't set all of it
            environment["PATH"] = extra + ":" + (environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")
        }
        process.environment = environment
        process.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser
        let output = Pipe()
        let input = Pipe()
        process.standardOutput = output
        process.standardInput = input
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self] finished in
            let status = finished.terminationStatus
            DispatchQueue.main.async { self?.serverStopped(status) }
        }
        do {
            try process.run()
        } catch {
            fail("Ixel couldn't start: \(error.localizedDescription)")
            return
        }
        server = process
        serverInput = input
        let reader = output.fileHandleForReading
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            // Your shell profile may print something first: the address is the line that starts with the prefix
            var buffer = Data()
            var address: URL?
            reading: while address == nil {
                let chunk = reader.availableData
                if chunk.isEmpty { break reading }
                buffer.append(chunk)
                while let newline = buffer.firstIndex(of: 0x0A) {
                    let line = String(decoding: buffer[buffer.startIndex..<newline], as: UTF8.self)
                    buffer.removeSubrange(buffer.startIndex...newline)
                    if line.hasPrefix(hostPrefix),
                       let url = URL(string: String(line.dropFirst(hostPrefix.count))),
                       url.scheme == "http", url.host == "127.0.0.1" {
                        address = url
                        break
                    }
                }
            }
            if let address = address {
                DispatchQueue.main.async {
                    self?.address = address  // first: the load below is checked against it
                    self?.webView.load(URLRequest(url: address))
                }
            }
            while !reader.availableData.isEmpty {}  // keep the pipe drained until the server exits
        }
    }

    private func stopServer() {
        quitting = true
        guard let server = server, server.isRunning else { return }
        try? serverInput?.fileHandleForWriting.close()  // end of input: the server stops by itself
        let deadline = Date().addingTimeInterval(5)
        while server.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        if server.isRunning {
            server.terminate()
        }
    }

    private func serverStopped(_ status: Int32) {
        if quitting { return }
        fail("Ixel stopped (exit code \(status)). Open it again. If that keeps happening, run `ixel gui` in "
             + "Terminal to see why.")
    }

    private func loginShell() -> String {
        if let entry = getpwuid(getuid()), let shell = entry.pointee.pw_shell {
            let path = String(cString: shell)
            let name = (path as NSString).lastPathComponent
            if ["zsh", "bash", "sh"].contains(name) && FileManager.default.isExecutableFile(atPath: path) {
                return path
            }
        }
        return "/bin/zsh"
    }

    private func fail(_ message: String) {
        quitting = true
        let alert = NSAlert()
        alert.messageText = "Ixel"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.runModal()
        NSApp.terminate(nil)
    }

    // ── Menus: Edit is what makes Cmd-C and Cmd-V work in the page ─────────

    private func makeMenu() -> NSMenu {
        let main = NSMenu()

        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Ixel",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Hide Ixel", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit Ixel", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        main.addItem(submenu(appMenu))

        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(NSMenuItem.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        main.addItem(submenu(edit))

        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Reload", action: #selector(WKWebView.reload(_:)), keyEquivalent: "r")
        main.addItem(submenu(view))

        let windows = NSMenu(title: "Window")
        windows.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windows.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        main.addItem(submenu(windows))
        NSApp.windowsMenu = windows

        return main
    }

    private func submenu(_ menu: NSMenu) -> NSMenuItem {
        let item = NSMenuItem()
        item.submenu = menu
        return item
    }

    // ── Settings > Appearance: the title bar and the file chooser take the page's colors ──

    func follow(_ choice: String?) {
        switch choice {
        case "light"?: NSApp.appearance = NSAppearance(named: .aqua)
        case "dark"?: NSApp.appearance = NSAppearance(named: .darkAqua)
        default: NSApp.appearance = nil  // System: the Mac's, switching when it does
        }
    }

    // What the page will say once it's loaded, from the file Ixel saves it in (appearance.py)
    private func savedAppearance() -> String? {
        let file = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".config/ixel-mat/app.json")
        guard let data = try? Data(contentsOf: file),
              let saved = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        return saved["appearance"] as? String
    }
}

// ── Settings > Appearance: the page says system, light or dark as it opens and when you change it ──

extension AppDelegate: WKScriptMessageHandler {
    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        follow(message.body as? String)
    }
}

// ── Choosing files: Ask's Picture and Sound buttons (WebKit shows no file chooser by itself) ──

extension AppDelegate: WKUIDelegate {
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.beginSheetModal(for: window) { response in
            completionHandler(response == .OK ? panel.urls : nil)
        }
    }
}

// ── Links: only Ixel's own page loads here; a link away from it opens in your browser ──

extension AppDelegate: WKNavigationDelegate {
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let url = navigationAction.request.url
        let scheme = url?.scheme?.lowercased()
        // A link with the download attribute saves a file (a recording you save is a blob: link). Only the
        // page itself starts one: not the sandboxed frame an add-on's diagram runs in.
        if navigationAction.shouldPerformDownload, navigationAction.sourceFrame.isMainFrame,
           isIxelPage(url) || scheme == "blob" || scheme == "data" {
            decisionHandler(.download)
        } else if isIxelPage(url) || url?.absoluteString == "about:blank" {
            decisionHandler(.allow)
        } else {
            // Only a click opens your browser: a script in the page can't open tabs by itself
            if navigationAction.navigationType == .linkActivated, let url = url, opensInBrowser(url) {
                NSWorkspace.shared.open(url)
            }
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        var disposition = ""
        if let http = navigationResponse.response as? HTTPURLResponse,
           let value = http.value(forHTTPHeaderField: "Content-Disposition") {
            disposition = value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        }
        if !navigationResponse.canShowMIMEType || disposition.hasPrefix("attachment") {
            decisionHandler(.download)
        } else {
            decisionHandler(.allow)
        }
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    // http to 127.0.0.1 on the server's port, with no name or password before the host
    // (http://127.0.0.1:80@evil.example/ is evil.example)
    private func isIxelPage(_ url: URL?) -> Bool {
        guard let url = url, let here = address, let port = url.port, port == here.port else { return false }
        return url.scheme?.lowercased() == "http" && url.host == "127.0.0.1"
            && url.user == nil && url.password == nil
    }

    // A web page or an email address; anything else (file:, or a scheme that starts some other program) is ignored
    private func opensInBrowser(_ url: URL) -> Bool {
        return ["http", "https", "mailto"].contains(url.scheme?.lowercased() ?? "")
    }
}

// ── Downloads: saved in your Downloads folder, under a name nothing there has ──

extension AppDelegate: WKDownloadDelegate {
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let files = FileManager.default
        let folder = files.urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? files.homeDirectoryForCurrentUser.appendingPathComponent("Downloads")
        try? files.createDirectory(at: folder, withIntermediateDirectories: true)
        let path = unusedPath(for: plainFilename(suggestedFilename), in: folder)
        downloads[download] = path
        completionHandler(path)
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard let path = downloads.removeValue(forKey: download) else { return }
        // What Safari posts, so the Downloads stack in the Dock bounces
        DistributedNotificationCenter.default().post(name: Notification.Name("com.apple.DownloadFileFinished"),
                                                     object: path.path)
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        let name = downloads.removeValue(forKey: download)?.lastPathComponent ?? "a file"
        NSLog("Ixel couldn't download %@: %@", name, error.localizedDescription)  // the page is told nothing
    }

    // What a page suggests, as one plain file name: nothing before a "/", no ":", no control characters, not hidden
    private func plainFilename(_ suggested: String) -> String {
        var name = suggested.components(separatedBy: "/").last ?? ""
        name = name.replacingOccurrences(of: ":", with: "-")
        name = name.components(separatedBy: .controlCharacters).joined(separator: " ")
        name = String(name.drop(while: { $0 == "." || $0.isWhitespace }))
        name = name.trimmingCharacters(in: .whitespacesAndNewlines)
        return name.isEmpty ? "Download" : name
    }

    // "name.ext", or "name (1).ext", "name (2).ext" and so on if that's taken
    private func unusedPath(for name: String, in folder: URL) -> URL {
        let base = (name as NSString).deletingPathExtension
        let ext = (name as NSString).pathExtension
        var path = folder.appendingPathComponent(name)
        var number = 1
        while isTaken(path) {
            path = folder.appendingPathComponent(ext.isEmpty ? "\(base) (\(number))" : "\(base) (\(number)).\(ext)")
            number += 1
        }
        return path
    }

    // WebKit fails a download whose file exists (a broken symlink counts), and two running at once would clash
    private func isTaken(_ path: URL) -> Bool {
        return (try? FileManager.default.attributesOfItem(atPath: path.path)) != nil
            || downloads.values.contains(path)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()

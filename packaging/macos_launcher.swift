import AppKit
import Foundation

final class AppDelegate: NSObject, NSApplicationDelegate {
    var server: Process?
    var quitting = false
    var output: FileHandle?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu()
        let item = NSMenuItem()
        menu.addItem(item)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit " + (Bundle.main.object(forInfoDictionaryKey: "CFBundleName") as? String ?? "Studio"),
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.submenu = appMenu
        NSApp.mainMenu = menu
        guard let resources = Bundle.main.resourceURL else { NSApp.terminate(nil); return }
        let process = Process()
        process.executableURL = resources.appendingPathComponent("runtime/bin/python3.13")
        process.arguments = [resources.appendingPathComponent("bootstrap.py").path]
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = resources.appendingPathComponent("libs").path + ":" + resources.appendingPathComponent("source").path
        process.environment = environment
        process.currentDirectoryURL = resources
        do {
            let folder = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Logs")
                .appendingPathComponent(Bundle.main.bundleIdentifier ?? "PWStudio")
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            let log = folder.appendingPathComponent("studio.log")
            if !FileManager.default.fileExists(atPath: log.path) {
                FileManager.default.createFile(atPath: log.path, contents: nil)
            }
            output = try FileHandle(forWritingTo: log)
            try output?.seekToEnd()
            process.standardOutput = output
            process.standardError = output
            process.terminationHandler = { [weak self] _ in
                DispatchQueue.main.async {
                    if self?.quitting == true {
                        NSApp.reply(toApplicationShouldTerminate: true)
                    } else {
                        NSApp.terminate(nil)
                    }
                }
            }
            server = process
            try process.run()
        } catch {
            let alert = NSAlert()
            alert.messageText = "The application could not start."
            alert.informativeText = error.localizedDescription
            alert.runModal()
            NSApp.terminate(nil)
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let process = server, process.isRunning else { return .terminateNow }
        quitting = true
        process.terminate()
        return .terminateLater
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()


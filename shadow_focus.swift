import AppKit
// Activate exactly one process; no AppleScript or Accessibility permission needed.
guard CommandLine.arguments.count >= 2,
      let pid = Int32(CommandLine.arguments[1]),
      let app = NSRunningApplication(processIdentifier: pid) else { exit(2) }
if CommandLine.arguments.count == 3 && CommandLine.arguments[2] == "--quit" {
    exit(app.terminate() ? 0 : 4)
}
exit(app.activate(options: [.activateAllWindows]) ? 0 : 3)

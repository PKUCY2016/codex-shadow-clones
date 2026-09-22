import AppKit
import Foundation

// Reload only this checkout's menu companion. No Codex desktop is terminated.
guard CommandLine.arguments.count == 2 else { exit(2) }
let target = URL(fileURLWithPath: CommandLine.arguments[1]).standardizedFileURL
let matches = NSRunningApplication.runningApplications(withBundleIdentifier: "local.codex.shadow-clones.menubar").filter {
    $0.bundleURL?.standardizedFileURL == target
}
for app in matches {
    guard app.terminate() else {
        fputs("无法退出旧菜单栏，请从忍者菜单选择退出后重试。\n", stderr)
        exit(1)
    }
}
let deadline = Date().addingTimeInterval(10)
while matches.contains(where: { !$0.isTerminated }) && Date() < deadline {
    RunLoop.current.run(until: Date().addingTimeInterval(0.1))
}
guard matches.allSatisfy({ $0.isTerminated }) else { exit(3) }
let config = NSWorkspace.OpenConfiguration()
config.activates = false
NSWorkspace.shared.openApplication(at: target, configuration: config) { app, error in
    guard app != nil, error == nil else {
        fputs("新版菜单栏启动失败，请重新运行启动器。\n", stderr)
        exit(4)
    }
    print("新版菜单栏已加载。")
    exit(0)
}
RunLoop.current.run(until: Date().addingTimeInterval(10))
exit(5)

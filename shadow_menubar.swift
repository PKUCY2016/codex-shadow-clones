import AppKit
import Foundation
import Darwin

// Native companion to the loopback manager. Credentials never enter arguments or logs.
final class ShadowMenu: NSObject, NSApplicationDelegate {
    private var item: NSStatusItem!
    private var timer: Timer?
    private var state: [String: Any] = [:]
    private var message = "正在连接管理服务…"
    private var fetching = false
    private var attemptedStart = false
    private var children: [Process] = []
    private let root = Bundle.main.object(forInfoDictionaryKey: "ShadowProjectRoot") as? String ?? ""
    private let python = Bundle.main.object(forInfoDictionaryKey: "ShadowPython") as? String ?? ""
    private let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 5
        config.connectionProxyDictionary = [:]
        return URLSession(configuration: config)
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let ninjas = NSImage(size: NSSize(width: 32, height: 20), flipped: false) { _ in
            for (x, y, size) in [(1.0, 6.0, 13.0), (18.0, 6.0, 13.0), (8.0, 1.0, 16.0)] {
                NSColor.black.setFill()
                NSBezierPath(ovalIn: NSRect(x: x, y: y, width: size, height: size)).fill()
                NSGraphicsContext.saveGraphicsState()
                NSGraphicsContext.current?.cgContext.setBlendMode(.clear)
                NSBezierPath(roundedRect: NSRect(x: x + size * 0.15, y: y + size * 0.42,
                    width: size * 0.70, height: size * 0.22), xRadius: 1, yRadius: 1).fill()
                NSGraphicsContext.restoreGraphicsState()
                NSColor.black.setFill()
                for eye in [0.28, 0.60] {
                    NSBezierPath(ovalIn: NSRect(x: x + size * eye, y: y + size * 0.46,
                        width: size * 0.12, height: size * 0.12)).fill()
                }
            }
            return true
        }
        ninjas.isTemplate = true
        ninjas.accessibilityDescription = "三个忍者 · Codex 影分身"
        item.button?.image = ninjas
        item.button?.imagePosition = .imageLeading
        item.button?.title = " 影"
        item.button?.toolTip = "Codex 影分身 · 切换窗口与查看额度"
        rebuild()
        fetch()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in self?.fetch() }
    }

    private func credentials() -> String? {
        let path = root + "/.runtime/shadow-server.json"
        var metadata = stat()
        guard lstat(path, &metadata) == 0, metadata.st_uid == getuid(),
              (metadata.st_mode & mode_t(S_IFMT)) == mode_t(S_IFREG),
              (metadata.st_mode & 0o077) == 0, metadata.st_size < 8192,
              let data = FileManager.default.contents(atPath: path),
              let info = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              (info["port"] as? Int) == 18318,
              let token = info["token"] as? String,
              token.count >= 32, token.count < 256 else { return nil }
        return token
    }

    private func request(_ path: String, body: [String: Any]? = nil,
                         completion: @escaping ([String: Any]?, Bool) -> Void) {
        guard let token = credentials(), let url = URL(string: "http://127.0.0.1:18318" + path) else {
            completion(nil, false)
            return
        }
        var request = URLRequest(url: url)
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        if let body = body {
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        session.dataTask(with: request) { data, response, _ in
            let payload = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let success = (response as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async { completion(payload, success) }
        }.resume()
    }

    private func fetch() {
        guard !fetching else { return }
        fetching = true
        request("/api/state") { [weak self] payload, success in
            guard let self = self else { return }
            self.fetching = false
            if success, let payload = payload {
                self.state = payload
                self.message = payload["working"] as? Bool == true ? (payload["message"] as? String ?? "正在复制历史…") : (payload["refreshing"] as? Bool == true ? "正在查询账号额度…" : "额度每 120 秒更新")
            } else {
                self.state = [:]
                self.message = "管理服务未连接 · 点击打开管理面板"
                if !self.attemptedStart {
                    self.attemptedStart = true
                    self.runPython("serve")
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.fetch() }
                }
            }
            self.rebuild()
        }
    }

    private func add(_ menu: NSMenu, _ title: String, _ action: Selector? = nil, id: String? = nil) {
        let entry = NSMenuItem(title: title, action: action, keyEquivalent: "")
        entry.target = self
        entry.representedObject = id
        if action == nil { entry.isEnabled = false }
        menu.addItem(entry)
    }

    private func rebuild() {
        let menu = NSMenu()
        add(menu, "Codex 影分身")
        add(menu, message)
        menu.addItem(.separator())
        for profile in state["profiles"] as? [[String: Any]] ?? [] {
            guard let id = profile["id"] as? String, let name = profile["name"] as? String else { continue }
            let quota = profile["quota"] as? [String: Any] ?? [:]
            let status = quota["status"] as? String ?? "unknown"
            var amount = status == "not_logged_in" ? "未登录" : "额度未知"
            if status == "ok", let remaining = quota["coreRemainingPercent"] as? Double {
                let checked = quota["checkedAt"] as? Double ?? 0
                amount = String(format: "剩余 %.0f%%", remaining)
                if Date().timeIntervalSince1970 - checked > 300 { amount += "（旧数据）" }
            }
            let selected = state["selected"] as? String == id ? "✓ " : ""
            let running = profile["running"] as? Bool == true ? " · 已打开" : ""
            add(menu, selected + name + " · " + amount + running, #selector(openProfile(_:)), id: id)
        }
        menu.addItem(.separator())
        add(menu, "刷新账号额度", #selector(refreshQuota))
        add(menu, "创建新分身…", #selector(createClone))
        add(menu, "打开管理面板", #selector(openDashboard))
        menu.addItem(.separator())
        add(menu, "切换窗口不会迁移正在运行的任务")
        add(menu, "退出菜单栏（保留分身与监控）", #selector(quit))
        item.menu = menu
    }

    private func perform(_ body: [String: Any]) {
        request("/api/action", body: body) { [weak self] payload, success in
            guard let self = self else { return }
            if success { self.fetch() }
            else {
                self.message = payload?["error"] as? String ?? "操作失败，请打开管理面板检查"
                self.rebuild()
            }
        }
    }

    private func runPython(_ action: String) {
        guard !root.isEmpty, !python.isEmpty else { return }
        children.removeAll { !$0.isRunning }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = [root + "/shadow_clones.py", action]
        process.currentDirectoryURL = URL(fileURLWithPath: root)
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do { try process.run(); children.append(process) }
        catch { message = "管理服务启动失败，请重新运行项目启动入口"; rebuild() }
    }

    @objc private func openProfile(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        perform(["action": "open", "id": id])
    }
    @objc private func refreshQuota() { perform(["action": "refresh"]) }
    @objc private func createClone() { perform(["action": "create"]) }
    @objc private func openDashboard() { runPython("dashboard") }
    @objc private func quit() { NSApp.terminate(nil) }
}

let app = NSApplication.shared
let delegate = ShadowMenu()
app.delegate = delegate
app.run()

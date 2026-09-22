# Codex 3 Computer Use 验证

本轮实际调用证明原生应用读取和截图可用。`cua.getState()` 曾多次超时，不能据此推断整个 Computer Use 不可用。

针对已知目标应用，使用插件文档提供的直接入口：

```javascript
let finder = await cua.getApp('com.apple.finder');
```

阅读返回文档和界面后，再调用：

```javascript
const shot = await finder.getScreenshot({emit:false});
nodeRepl.write({screenshot_received:shot.byteLength > 0});
```

本次返回 55,475 字节 JPEG，文件头符合 JPEG/JFIF。截图内容未输出、未写入凭证。跨运行环境的 `instanceof Uint8Array` 可能为 false，不能用它单独判定截图失败。

没有修改权限、配置或应用二进制，没有重启应用。这是已经验证可用的直接连接路径，尚未修复或查明综合枚举的底层超时原因；浏览器操作及点击输入也未验收。

脱敏回执：`.runtime/codex3-computer-use-repair.json`。

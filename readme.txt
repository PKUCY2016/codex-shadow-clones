Codex 影分身 / Codex Shadow Clones
https://github.com/PKUCY2016/codex-shadow-clones

用途：macOS 多个 Codex 桌面实例，各自登录，共享项目代码，复制配置和已结束的历史快照，查看额度并切换窗口。不是订阅合并或运行中任务迁移。

【直接复制以下内容交给 Codex】

请帮我在本机安装并启动这个仓库：
https://github.com/PKUCY2016/codex-shadow-clones

1. 如果还没检出仓库，先 git clone 到一个新目录；不要覆盖现有目录或删除用户文件。阅读 README.md、docs/USAGE.md 和当前源代码。
2. 先检查 uname -s、python3 --version、xcode-select -p、swiftc --version。要求 macOS、Python 3.11+、Xcode Command Line Tools。缺依赖时报告实际缺项，不假称已安装。Python 仅使用标准库，无需 pip install。
3. 只读取 /Applications/ChatGPT.app/Contents/Info.plist 核验 Bundle ID、版本和 build。当前支持 com.openai.codex、26.915.31945、9922；同时核对 desktop_second.py 的 CHECKED。如果不匹配，停止启动并报告，不删除版本检查、不改应用签名、不假定新版兼容。
4. 默认源目录是 ~/.codex。如我使用其他原实例目录，先报告当前不支持的差异，不擅自改全局 HOME 或替换原账号。不要读取、打印、上传或要求我粘贴 auth.json、令牌、授权链接、私人会话。不要把 .runtime 内容提交到 Git。
5. 在仓库先运行只读预检，再运行离线测试：
   python3 scripts/preflight.py
   python3 -m unittest discover -s tests -p 'test_shadow*.py' -v
   测试失败先报告并定位，不跳过失败项后宣称通过。
6. 检查通过后运行：
   python3 launch_shadow.py
   它会本地编译菜单栏并打开管理面板。不能仅凭进程存在声称所有功能验收完成。
7. 引导我在管理面板创建分身，然后由我本人在官方窗口登录另一个账号。不要代填密码，不索要令牌，不同时发起多个首次登录流程。先保持自动切换关闭。
8. 切换窗口后确认账号状态、项目入口和额度是否可读；失败与未知状态要明确报告。不要为测试额度而额外发起模型请求。
9. 更新目标分身的历史或配置前，必须确认该目标已退出。不要关闭有进行中任务的应用。配置以源快照为准；统一历史按内容前缀快进，独立变化保留分支副本。源当前未结束回合暂不复制。不要手动覆盖正在写入的 SQLite 数据库。
10. 最后报告：实际检查结果、测试结果、管理入口是否启动、仍需我完成的登录或验证，以及版本/历史限制。不要声称自动切换会转移当前运行任务。

【日常使用】
双击 Codex影分身.command，或在仓库运行 python3 launch_shadow.py。
在菜单栏查看各分身余量；在面板选择切换窗口。
删除分身前先在目标按 ⌘Q 退出，再在卡片点击删除并确认；数据移入本地备份，原实例不可删除。
v0.4.0 起会自动检查新版。旧安装首次需要按 README 更新步骤拉取代码、预检和测试，再运行 python3 launch_shadow.py --restart。新建分身使用短物理 home；本地记忆快照和暂停的自动化模板会随配置复制，账号登录仍由你本人完成。
点击统一同步历史，汇总所有实例已结束的回合。运行中的实例延后，退出后补齐；可开启定期汇总。
同步配置仍须先退出目标分身；配置来源仍为原实例。
普通历史分叉保留独立副本；分页依赖类型会显示不支持，不要强制覆盖。
直接打开 localhost 地址可能 Unauthorized，请用启动入口，不复制访问令牌。
关闭窗口不等于退出，使用目标应用的退出操作。退出菜单栏也不会关闭后台监控。

【能力边界】
每个账号消耗自己的额度。共享代码文件，独立聊天与账号状态。
配置保持独立快照；历史可从所有实例汇总并分发到退出的实例。第三方插件登录不复制。
部分旧历史可能无法分页显示，保留源实例与备份。
自动切换默认关闭；开启后切的是窗口，不是运行中的任务。
不支持 Windows / Linux，桌面版本升级需重新适配。

【参考与许可】
liuzhao1225/codex-account-switcher：官方 app-server 身份/额度查询方式。
lordydord/Codex-Account-Switcher：低额度阈值、冷却与候选账号选择思路。
独立实现，不是上述项目 fork，没有复制其源码。完整来源在 docs/REFERENCES.md。
MIT，见 LICENSE。社区项目，非 OpenAI 官方产品。

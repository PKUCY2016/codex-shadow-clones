# 来源与致谢

核查日期：2026-09-22。下面区分实现思路、官方接口与文档编排来源；未把参考项目的全部功能归为影分身已实现能力。

## 功能与协议参考

| 来源 | 借鉴内容 | 对应本项目 | 没有采用的部分 |
|---|---|---|---|
| [liuzhao1225/codex-account-switcher](https://github.com/liuzhao1225/codex-account-switcher)，MIT | 按独立 `CODEX_HOME` 启动官方 app-server，通过 `account/read`、`account/rateLimits/read` 查询身份与额度 | `shadow_quota.py` | 不使用其凭据替换、单实例退出/重启交接或原生 UI 源码 |
| [lordydord/Codex-Account-Switcher](https://github.com/lordydord/Codex-Account-Switcher)，MIT | 低额度阈值、候选筛选与冷却的产品思路 | `shadow_clones.py` 的可选自动窗口切换 | 不依赖其 `codex-auth`，不采用自动粘贴继续提示或 reset-credit 消费 |
| [OpenAI App Server 文档](https://learn.chatgpt.com/docs/app-server) | 官方账号、额度与项目 RPC | `shadow_quota.py`、`shadow_seed.py` | 不将桌面内部目录或历史数据库结构宣称为官方稳定迁移 API |

固定源码定位：

- [liuzhao / CodexClient.swift @ a371af1](https://github.com/liuzhao1225/codex-account-switcher/blob/a371af174defd4e579424286e71f2c08db1f3514/Sources/SwitcherCore/CodexClient.swift)：身份和额度查询协议。
- [lordydord / main.swift @ b223b24](https://github.com/lordydord/Codex-Account-Switcher/blob/b223b24b05398f51a5194d253df2ba3aff05af81/Sources/main.swift)：账号切换与自动化流程。其 README 也明确列出阈值选择与防止频繁切换的冷却。

本项目独立编写，不是这两个仓库的 fork，没有复制其源码。新增实现包括多个并存桌面实例、独立登录和运行数据、共享原项目目录、项目/配置导入、可更新的历史快照、目标独立变化的冲突保留、面板与原生菜单栏统一控制。并存窗口与账号池是不同架构，每个账号额度仍独立计算。

## README 编排参考

本次选择两个具有代表性的高关注度开源项目作为文档结构样本，没有声称研究“所有高星 GitHub 项目”。星数只是当日 GitHub 页面显示的近似数，不作为兼容性或质量保证。

| 样本 | 2026-09-22 页面显示 | 借鉴的组织方式 |
|---|---|---|
| [astral-sh/uv README](https://github.com/astral-sh/uv#readme) | 约 90.0k stars | 一句话定位、先展示收益、安装与功能分层、文档和许可证入口 |
| [ollama/ollama README](https://github.com/ollama/ollama#readme) | 约 181.4k stars | 明确平台入口、短命令快速开始、具体使用示例与开发文档链接 |

此处仅参考信息组织方式，没有复制项目介绍或性能承诺。影分身自己的安装指令、能力、验证范围和限制来自本仓库源码及测试。`readme.txt` 补充给 Codex 的安装与验收指令，避免只给项目介绍而缺少可执行步骤。

## 标识与许可证

项目源码按 [MIT](../LICENSE) 发布。第三方名称与标识用于说明兼容对象和来源，归各自权利人所有。Codex Shadow Clones 是独立社区项目，不代表 OpenAI 或参考项目作者的官方产品与背书。

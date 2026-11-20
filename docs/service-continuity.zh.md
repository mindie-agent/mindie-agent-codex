# 已安装服务身份与连续性

<b>中文</b> | <a href="service-continuity.md">English</a> · <a href="README.md">返回文档索引</a>

`mindie-agent-codex` 将宿主插件分发与实际运行时解耦：OpenAI Codex CLI 负责管理插件目录与安装状态，而 `uv` 则在宿主易失缓存目录之外构建不可变的锁定 Python 运行时。

---

## 1. 脱离易失缓存的持久化运行时

- **为什么要脱离宿主缓存**：实机验证表明，当 Codex 刷新插件市场缓存时，可能会清理旧版本的缓存目录，而正在运行的 `app-server` 仍持有旧命令引用。将发行版入口（`entry.py`）与锁定运行时放置在持久化的 `PLUGIN_DATA` 目录下，可确保长会话期间的 `SessionStart`、`Stop` 与 MCP 调用始终稳定可达。
- **单一权威安装绑定**：用户的适配器配置文件（`codex.json`，对称支持 Windows `%APPDATA%/mindie-agent` 与 POSIX `XDG_CONFIG_HOME`）是当前生效运行时及 9 域内容寻址 `domain_configs`（`engines/codex/<20hex>/<domain>.engine.json`）的唯一权威绑定。稳定入口每次调用时按完整绑定分派对应的 Python 解释器与脚本，绝不跨版本混用模块或原地覆盖已准备好的运行时文件。
- **保持 Hook 信任哈希稳定与原生缓存零写入**：保持 `hooks/hooks.json` 字节级冻结并在源码 Python 启动路径显式加 `-B`、将基线 `uv sync` 放入系统临时目录（`%TEMP%` 下的 `mindie-build-*`）短生命副本中执行并清理，防止 `.pyc`、`build/` 与 `*.egg-info` 污染原生插件缓存目录，避免宿主因命令或缓存载荷变化而跳过已获用户信任的 `SessionStart`、`UserPromptSubmit`、`Stop` 或 `Interrupt` Hook。

---

## 2. 共享锁保护与空闲原子切换

- **在途请求持有代际租约**：常驻 MCP 进程在处理每次请求前受代际租约保护，确保在途操作在同一个一致的运行时中完成；运行时代际回收（`Active + Rollback <= 2`）唯一交由 `mindie-harness` 管理，`bootstrap.py` 绝不清理 `<data>/runtimes/codex/` 目录。
- **冷/热 `SessionStart` 标记握手与锁外候选构建**：冷/热 `SessionStart` 仅在 `ProfileRef.state_dir` 原子写入 `pending-update-check.json` 并在 `< 30ms` 内返回 `{}`，绝不同步执行 `uv sync` 或创建 SQLite。后台维护由 `mindie_harness.runtime.wheelhouse.update_runtime` 驱动，在锁外旁路构建候选 `uv` 运行时、继承全部 9 个运行时组件的版本下界并执行无模型探针校验，准备阶段不拉起知识后台服务，也不阻塞前台工具调用。
- **单一 `.codex.json.update.lock` 排他切换**：切换或重绑运行时统一获取 `.codex.json.update.lock` 排他锁，调用 `ensure_knowledge_binding(..., retire_previous=True, already_locked=True)` 确认旧服务空闲退役并生成内容寻址 9 域引擎配置后，再原子更新 `codex.json`；若服务正忙则自动推迟切换，无需用户手动重载会话。

---

## 3. 用户授权与代码版本解耦

- 社区知识贡献与自动工具故障上报共用与 `codex.json` 同目录的 `mindie-consent.json` 单一统一贡献开关，与批准的项目范围、已采集分块及用量账本一起作为持久化用户数据独立保存。
- 当用户显式开启统一贡献开关（带当前统一授权版本的 `contribute`）时，系统会配置知识共享并启动后台 reporter，同时严格区分三种状态：（1）本地授权配置已保存、（2）后台 reporter 进程运行中、（3）外部 GitHub Issue/PR 已实际创建。
- 插件升级或代码重载绝不会重新弹窗索要授权，不会修改已保存的 `disabled` / `later` / `read-only` 选择，也不会将缺少统一授权版本的旧版 `contribute` 记录（`needs_choice`）自动扩大为故障上报授权。

## 4. 0.1.8 / 0.1.9 首次迁移

0.1.10 的 MCP 启动器发现已安装的新版 seed 高于当前绑定时，会进入新版迁移控制器，等待旧调用释放操作锁，再通过既有原子切换路径发布完整绑定。两个 MCP 同时启动时共享有界等待，不会因 macOS 的文件锁竞争提前退出。Hook 命令保持稳定，Stop 不触发迁移。

已经常驻的 0.1.8 / 0.1.9 代码无法自行获得这个修复。旧更新器若已卡在激活回执，首次恢复需要先由原生安装器选中 0.1.10 正式 Release 的精确源码，再使用宿主支持的 MCP 刷新或自然重载，让新启动器接管。原生市场若仍固定在旧候选，直接重复 `plugin add` 会再次安装旧版本。不能用修改 canonical 版本号、删除锁文件或覆盖运行时文件代替迁移。

原生安装、运行时准备、绑定生效和宿主加载新工具是四个不同的验证点。已完成的旧候选在原生缓存被清理后仍可按回执归档；本地激活中断通过不可变尝试快照与绑定读回恢复，不再被旧 `unknown` 永久阻塞。结果未确认的原生安装或市场写入仍不会自动重放。

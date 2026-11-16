# mindie-agent-codex 0.1.34：存储生命周期治理与组件基线对齐

[English](mindie-agent-codex-0.1.34.en.md)

- **Wave 1 正式版组件基线（`plugins/mindie-agent/pyproject.toml`、`uv.lock`）**：将 `mindie-knowledge`、`mindie-diagnostics`、`remote-dev`、`mindie-coordinator`、`mindie-profiler`、`mindie-sim`、`mindie-debugger` 全部锁定到已审查的正式版 commit，并重新生成 `mcp_catalog.json`，保证发现目录与派发契约来自同一运行时。

- **升级只构建一套受管 `.venv`（`bootstrap.py`、`component_update.py`）**：当已有配置存在组件 floor 且其解释器健康时，先由现有解释器探测/构建合并组件运行时；产出了 `component-runtimes/<key>` 就直接复用，不再先构建随后被淘汰的孤儿 baseline 运行时。仅在无需组件覆盖时才构建单套 baseline，升级路径不再占用双倍磁盘；切换到 baseline 时只清理旧 floor 的受管组件命令，用户自定义 wrapper 原样保留。

- **存量历史包袱自动静默回收（`bootstrap.py`、`component_update.py`、`release_activation.py`、`harness_update.py`、`session_maintenance.py`、`storage_gc.py`）**：在构建前、切换完成后与会话维护周期内，按有界预算调用共享 `mindie_knowledge.storage` 的 `sweep_legacy_installations` 与 `prune_component_runtimes`，静默回收 10+ 代 `component-runtimes`、`runtimes`、旧 `releases/<old_ver>`、超量 `updates/superseded-*.json` 与 `prepare-*.log`；始终保留当前绑定、回滚前一代、最近验证代、存活租约/进程引用代与跨插件借用代。清理失败只记录 `cleanup_warning` / `deferred_in_use`，不改变已成功的主流程。

- **常驻 MCP 持有共享运行租约（`entry.py`）**：`knowledge-mcp`、`remote-mcp`、`tools-mcp` 与 `bridge mcp` 在服务期间对当前受管代目录持有 `acquire_runtime_lease`，POSIX 与 Windows 上的后台回收都不会移动正在服务会话的运行时。

- **共享同卷 uv 缓存与受限安装日志（`bootstrap.py`、`storage_gc.py`）**：调用方未显式设置 `UV_CACHE_DIR` 且默认缓存跨卷时，改用同卷受管 `<data>/.uv-cache`；成功安装后立即删除本次 `prepare-*.log`，失败时仅保留最近有界诊断日志，避免日志无限堆积。

- **`RemoteReceipts` owner 锁与采集 `.active` 回收（`mcp_gate.py`、`receipt_owners.py`、`capture_spool.py`、`mcp_transport.py`）**：释放 `_ownership` 时在非阻塞独占确认无竞争后清理自身 `<owner>.owner.lock`，初始化时按有界扫描回收已死 owner 的历史锁文件；`Capture` 创建与清扫目录时回收 `>24h` 且未被本进程打开的陈旧孤儿 `*.active`，`_abandon()` 也会清理自己已创建但无用的临时文件。

- **跨平台约束**：Windows 不使用 `os.kill(pid, 0)`，目录重命名前不持有目标目录内的打开句柄，路径等价比较统一使用 `mindie_knowledge.filesystem.path_identity`；全部文本文件保持 Unix `LF`。

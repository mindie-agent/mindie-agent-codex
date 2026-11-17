# mindie-agent-codex 0.1.35：接入 `mindie-harness` 共享底座、只读 MCP 并发解耦与 `0.1.13` 组件基线对齐

[English](mindie-agent-codex-0.1.35.en.md)

- **接入 `mindie-harness v0.1.0` 纯标准库共享底座**：内置确定性冷启动包 `assets/mindie-harness-seed.pyz` 与 `seed-lock.json`（`mindie-harness-seed/1`），将共享文件锁、RSI 实验账本/循环引擎（`experiment_ledger.py`、`rsi_loop.py`、`rsi_explore.py`）与宿主适配器契约统一委托至 `mindie_harness`。
- **只读 MCP 并发解耦与反模式清理（反转 `AD-07/09/10/12`）**：解除只读/状态查询工具与慢速写调用之间的串行阻塞，支持开发者本地 symlink 模式挂载，消除 `TaskRootBusy` 类本地单向自锁死分支，并保持冻结 `hooks/hooks.json` 字节摘要完全不变。
- **`0.1.13` 全量组件正式版锁定**：在 `plugins/mindie-agent/pyproject.toml` 与 `uv.lock` 中同步锁定 `mindie-harness v0.1.0`、`mindie-knowledge v0.13.23`、`remote-dev v0.11.13`、`mindie-diagnostics v0.5.16`、`mindie-debugger v0.1.4`、`mindie-profiler v0.5.16`、`mindie-sim v0.9.18` 与 `mindie-coordinator v0.7.13` 正式标签提交，并对齐 `--profile host-plugin` PR 设计原则门禁。

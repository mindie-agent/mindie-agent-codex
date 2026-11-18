# MindIE Agent Codex v0.1.39 — Pre-`0.1.38` 在线会话与自升级交接加固

[English](mindie-agent-codex-0.1.39.en.md)

- **存量 `v0.1.34`–`v0.1.37` 活跃会话热切流兼容（修复 [`mindie-agent#381`](https://github.com/mindie-agent/mindie-agent/issues/381)、[`mindie-agent#382`](https://github.com/mindie-agent/mindie-agent/issues/382)）**：补齐轻量 `harness_update.py`、`runtime_call.py` 与 `admission_ops.py` 交接桩，确保 `codex.json` 切流至 `0.1.39+` 后尚未重启的 `v0.1.34`–`v0.1.37` 常驻 MCP 进程与 `update_trigger._run` 后台维护线程零报错平滑运行。
- **跨平台 `bootstrap.ensure_runtime` 与旧服务确认退役加固（修复 [`mindie-agent#379`](https://github.com/mindie-agent/mindie-agent/issues/379)、[`mindie-agent#380`](https://github.com/mindie-agent/mindie-agent/issues/380)）**：构建临时目录改用系统短路径 `%TEMP%` 避免 Windows `MAX_PATH` 溢出，移除强制 `http.sslBackend=schannel` 覆盖，在 `_restore_floors` 中正确排除 `mindie-agent-codex` 并支持 `.whl` 直链，修复 POSIX 虚拟环境软链保留逻辑（绝不在升级中误删正在执行的旧代 venv），并在 `release_activation.py` 中要求确认旧知识服务空闲退役后再发布 `codex.json`。
- **自动化 `n-1 -> n` 自升级契约测试**：新增 `tests/test_legacy_self_upgrade.py` 端到端覆盖 `harness_update.py`、`runtime_call.py`（`catalog` 与工具调用）、`admission_ops.py`、POSIX 软链保留与退役门禁。

# 故障诊断与取消边界

中文 | [English](failure-diagnostics.md) · [返回文档索引](README.md)

本文说明 `mindie-agent-codex` 的只读诊断查询、本地调用取消与远端持久作业边界。完整英文细则见 [`failure-diagnostics.md`](failure-diagnostics.md)。

## 核心规则

- **纯只读状态检查**：`status` 与 `reporting-status` 仅读取本地配置、服务与账本事实，不启动后台服务、不调用模型、不重放失败操作。
- **本地取消与远端持久作业分离**：取消前台工具调用或遇到标准输入 EOF 时，立即回收本次调用拥有的本地辅助进程树；而通过 `remote-dev` 创建的远端持久后台作业保留其独立作业 ID 与生命周期，供调用方后续查询或显式取消。

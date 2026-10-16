# 框架稳定性与持久化配置

中文 | [English](framework-stability.md) · [返回文档索引](README.md)

本文说明 `mindie-agent-codex` 的持久化配置、Windows 原子配置读写与组件测试基线。完整英文细则见 [`framework-stability.md`](framework-stability.md)。

## 核心保障

- **持久化用户选择**：社区贡献选择、项目范围与独立故障上报开关跨任务、Fork、重启与插件升级持久保持；损坏的配置文件直接作为可见故障报告，绝不静默重置为首次安装。
- **Windows 原子配置读写**：结合删除共享读取与 Windows 原生原子重命名语义，确保并发读取者始终看到完整的配置文档。

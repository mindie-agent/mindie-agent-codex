# MindIE Agent Codex 插件文档索引

中文 | [English](README.en.md) · [返回仓库首页](../README.md)

本目录汇总 `mindie-agent-codex`（OpenAI Codex CLI 原生插件）的宿主接口兼容性证据、公开对话采集、历史会话导入、服务连续性、故障诊断、框架稳定性与测试契约文档。所有文档均提供中英双语版本。

## 核心技术文档

| 中文文档 | English | 说明 |
|---|---|---|
| [Codex Harness 接口兼容性证据](harness-interfaces.zh.md) | [Harness Interface Evidence](harness-interfaces.md) | `harness-compatibility.json` 登记接口、原生管理、主线程中断与子代理取消已知限制 |
| [公开对话采集与脱敏](public-transcript.zh.md) | [Public Transcript Capture](public-transcript.md) | 原生 `response_item` 筛选、Gitleaks 8.30.1 增量扫描与不可变分块提交 |
| [显式历史会话导入](history-import.zh.md) | [Explicit Historical Contribution](history-import.md) | 显式指定 JSONL 历史会话导入的权限校验与增量脱敏流程 |
| [已安装服务身份与连续性](service-continuity.zh.md) | [Installed Service Identity](service-continuity.md) | 脱离易失缓存的锁定运行时、单一配置绑定与空闲切换契约 |
| [故障诊断与取消边界](failure-diagnostics.zh.md) | [Failure Diagnostics](failure-diagnostics.md) | 只读状态诊断、本地进程组取消清理与远端持久作业分离 |
| [框架稳定性与持久化配置](framework-stability.zh.md) | [Framework Stability](framework-stability.md) | 日常使用持久选择、Windows 原子配置读写与验收边界 |
| [测试边界与契约校验](testing-contracts.zh.md) | [Test Boundaries & Contracts](testing-contracts.md) | `tests/preflight.py` 预检、固定依赖提交校验与多维契约测试矩阵 |

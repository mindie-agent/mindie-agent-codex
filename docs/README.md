# MindIE Agent for OpenAI Codex CLI · 技术文档索引

<b>中文</b> | <a href="README.en.md">English</a> · <a href="../README.md">返回仓库首页</a>

本目录汇总 `mindie-agent-codex`（OpenAI Codex CLI 原生插件）的宿主接口兼容性验证、公开对话采集脱敏、历史会话导入、服务连续性、故障诊断与测试契约文档。所有文档均提供结构对称的中英双语版本。

## 核心文档列表

| 中文文档 | English | 内容概要 |
|---|---|---|
| [Codex Harness 接口兼容性证据](harness-interfaces.zh.md) | [Harness Interface Evidence](harness-interfaces.md) | `harness-compatibility.json` 登记接口、原生插件管理、主线程 `Interrupt` 取消与子代理取消已知限制 |
| [公开对话采集与本地脱敏](public-transcript.zh.md) | [Public Transcript Capture](public-transcript.md) | 原生 `response_item` 筛选、Gitleaks `8.30.1` 增量扫描、不可变分块落盘与隔离索引生成 |
| [显式历史会话导入](history-import.zh.md) | [Explicit Historical Contribution](history-import.md) | 显式导入选定 JSONL 历史会话的授权核验、增量脱敏与断点续传流程 |
| [已安装服务身份与连续性](service-continuity.zh.md) | [Installed Service Identity](service-continuity.md) | 脱离易失缓存的锁定运行时、单一配置绑定与空闲原子切换机制 |
| [故障诊断与取消边界](failure-diagnostics.zh.md) | [Failure Diagnostics](failure-diagnostics.md) | 纯只读状态诊断、本地进程树取消回收与远端持久作业分离 |
| [框架稳定性与持久化配置](framework-stability.zh.md) | [Framework Stability](framework-stability.md) | 日常无感使用保障、Windows 原子配置读写与组件测试复现说明 |
| [测试边界与契约校验](testing-contracts.zh.md) | [Test Boundaries & Contracts](testing-contracts.md) | `tests/preflight.py` 预检、发布兼容性校验与多维契约测试矩阵 |

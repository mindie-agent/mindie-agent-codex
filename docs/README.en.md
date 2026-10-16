# MindIE Agent for OpenAI Codex CLI · Documentation Index

<a href="README.md">中文</a> | <b>English</b> · <a href="../README.en.md">Back to Repository Home</a>

This directory contains the Harness interface verification records, public transcript capture pipeline, historical session import guide, service continuity design, failure diagnostics, and testing contract specifications for `mindie-agent-codex`. Every document is available in both English and Chinese.

## Technical Guides

| English Document | 中文 | Summary |
|---|---|---|
| [Harness Interface Evidence](harness-interfaces.md) | [Codex Harness 接口兼容性证据](harness-interfaces.zh.md) | Registered interfaces in `harness-compatibility.json`, native management, main-thread `Interrupt` hook, and child-agent limitation |
| [Public Transcript Capture](public-transcript.md) | [公开对话采集与本地脱敏](public-transcript.zh.md) | Native `response_item` filtering, Gitleaks `8.30.1` incremental scanning, immutable Markdown blocks, and isolated indexing |
| [Explicit Historical Contribution](history-import.md) | [显式历史会话导入](history-import.zh.md) | Consent verification, incremental redaction, and resumable import for selected historical JSONL transcripts |
| [Installed Service Identity](service-continuity.md) | [已安装服务身份与连续性](service-continuity.zh.md) | Locked runtime outside disposable caches, single canonical binding, and atomic idle cutover |
| [Failure Diagnostics](failure-diagnostics.md) | [故障诊断与取消边界](failure-diagnostics.zh.md) | Read-only status checks, local process-group cancellation cleanup, and durable remote job separation |
| [Framework Stability](framework-stability.md) | [框架稳定性与持久化配置](framework-stability.zh.md) | Persistent user preferences across updates, Windows atomic config I/O, and verification boundaries |
| [Test Boundaries & Contracts](testing-contracts.md) | [测试边界与契约校验](testing-contracts.zh.md) | `tests/preflight.py` checks, publication compatibility validation, and contract test matrix |

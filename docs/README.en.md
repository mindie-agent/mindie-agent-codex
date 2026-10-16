# MindIE Agent Codex Plugin Documentation Index

[中文](README.md) | English · [Back to Repository Home](../README.en.md)

This directory contains the Harness interface evidence, public transcript capture, historical import, service continuity, failure diagnostics, framework stability, and testing contract specifications for `mindie-agent-codex`. All documents are available in both English and Chinese.

## Technical Specifications

| English Document | 中文 | Description |
|---|---|---|
| [Harness Interface Evidence](harness-interfaces.md) | [Codex Harness 接口兼容性证据](harness-interfaces.zh.md) | Registered interfaces in `harness-compatibility.json`, native management, main-thread interrupt, and child-agent limitation |
| [Public Transcript Capture](public-transcript.md) | [公开对话采集与脱敏](public-transcript.zh.md) | Native `response_item` projection, Gitleaks 8.30.1 incremental scanning, and immutable block persistence |
| [Explicit Historical Contribution](history-import.md) | [显式历史会话导入](history-import.zh.md) | Consent verification and incremental redaction pipeline for importing selected historical JSONL transcripts |
| [Installed Service Identity](service-continuity.md) | [已安装服务身份与连续性](service-continuity.zh.md) | Isolated locked runtime outside disposable caches, single canonical binding, and idle cutover |
| [Failure Diagnostics](failure-diagnostics.md) | [故障诊断与取消边界](failure-diagnostics.zh.md) | Read-only status diagnostics, local process-group cancellation cleanup, and durable remote job separation |
| [Framework Stability](framework-stability.md) | [框架稳定性与持久化配置](framework-stability.zh.md) | Persistent user consent across updates, Windows atomic config I/O, and verification boundaries |
| [Test Boundaries & Contracts](testing-contracts.md) | [测试边界与契约校验](testing-contracts.zh.md) | `tests/preflight.py` checks, pinned git revision verification, and contract test matrix |

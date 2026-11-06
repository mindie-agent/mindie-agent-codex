# mindie-agent-codex 0.1.21

[English](mindie-agent-codex-0.1.21.en.md)

本补丁修复 SSH 密码入口与任务连续性：用户已提供凭据时，通过共享 `remote_auth` 建立并验证可复用的密钥连接；知识、可选组件、引导和本地记账故障不阻断独立且已获授权的宿主工作。

远端主结果、失败与未知写入分别呈现。任务身份、权限、资源预约和活动作业继续保留；未知外部写入先对账再重试。验证覆盖公共 MCP 参数边界、真实 stdio 及隔离 SSH、跨平台 CI 和精确标签发布基准，不声称用户业务主机或 NPU 模型精度已重新验收。

本版也更新 RSI 停滞指引：记录三个不同的被证伪修复哈希后，由知识运行时自动从 `reuse` 晋升到 `explore`，同时遵守工作区边界。既有受控 CPU RSI A/B 实验仍归属于[产品 0.1.8 原始报告](https://github.com/mindie-agent/mindie-agent/blob/main/docs/releases/0.1.8/rsi-e2e-auto-switch-ab-report.md)，不重新标注为本补丁实验。

## 正式依赖组合

| Component | Version | Source commit |
| --- | --- | --- |
| knowledge | 0.13.13 | `3c478a19b5542b38520c7f98b87a07f37e305021` |
| remote-dev | 0.11.6 | `991ef7863b2b208f94f54a29db4ac5be50884ea3` |
| diagnostics | 0.5.7 | `f338788cecc56f7aff1dccab1d53426c9e28fe7f` |
| debugger | 0.1.0 | `e9406b2b178335190d5cb59dff4d6985894497ba` |
| profiler | 0.5.6 | `79d97e75bff7351b0472b93c03b072c1cf47f5e7` |
| sim | 0.9.4 | `6f1d5a871a7b1ef20ff7a8cab33aaf4637fb3f66` |
| coordinator | 0.7.5 | `819b0deb7bea994d4dff9c009200a48dd9300cc4` |

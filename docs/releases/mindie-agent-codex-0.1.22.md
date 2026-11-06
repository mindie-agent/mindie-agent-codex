# mindie-agent-codex 0.1.22

[English](mindie-agent-codex-0.1.22.en.md)

本补丁补齐组件独立升级后的 Codex MCP 工具目录：`remote_probe` 现在向模型暴露并接收 `distributions`、`python_executable`、`python_candidates` 和 `required_distributions`。此前组件运行时能够升级，但 Codex 固定目录缺少这些字段，调用会在宿主参数检查处被拒绝。

- 从锁定的 remote-dev 0.11.7 重新导出完整 Codex 目录，包括用户所选源码目录、业务解释器与任务脚本继续入口的共享说明。
- 引导环境锁定已正式发布的 remote-dev 0.11.7 和 coordinator 0.7.6；既有独立组件更新仍保留已安装版本下界。
- 四个参数可通过原生 MCP 参数校验并转发到共享探针。显式解释器失败不换环境，多个合格环境返回歧义，保留 venv 身份。
- 按宿主现有刷新边界加载新插件目录；组件绑定切换与宿主工具定义加载分别验证，活动会话继续保留。

验证覆盖目录生成、真实参数边界与运行时转发、旧安装更新、跨平台 CI 和精确 Tag 发布基准。A2/A3 原生重编译、动态库加载及 NPU Canary 不作为本补丁已完成验收。

## 正式依赖组合

| Component | Version | Source commit |
| --- | --- | --- |
| knowledge | 0.13.13 | `3c478a19b5542b38520c7f98b87a07f37e305021` |
| remote-dev | 0.11.7 | `6a64da8be9ed7c0bb38c2ba57f539da9a43ddf34` |
| diagnostics | 0.5.7 | `f338788cecc56f7aff1dccab1d53426c9e28fe7f` |
| debugger | 0.1.0 | `e9406b2b178335190d5cb59dff4d6985894497ba` |
| profiler | 0.5.6 | `79d97e75bff7351b0472b93c03b072c1cf47f5e7` |
| sim | 0.9.4 | `6f1d5a871a7b1ef20ff7a8cab33aaf4637fb3f66` |
| coordinator | 0.7.6 | `8690fdd065c73adab8dae2542d6a374f2ecab59e` |

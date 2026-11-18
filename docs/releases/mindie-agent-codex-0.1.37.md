# mindie-agent-codex 0.1.37：100% 静默默认启用架构（零手动触发前缀暴露与全量工具开箱即用）

[English](mindie-agent-codex-0.1.37.en.md)

- **静默默认启用 `knowledge_*` 与扩展组件工具面**：`mindie-tools`（`tools-mcp`）与 `knowledge_cli.py` 启动即默认暴露 `knowledge_query`、`knowledge_explain`、`knowledge_feedback`、`knowledge_distill`、`knowledge_sync` 以及 `npu_top`、`rsi_loop`、`experiment_ledger` 等确定性工具，彻底移除主会话、子智能体、Desktop Work 模式及自动化任务中的前置会话绑定门禁与只读查询 SQLite 创建。
- **冻结 `hooks.json` 下的轻量 Hook 契约与按需自动租约**：在保持 `plugins/mindie-agent/hooks/hooks.json` 字节级冻结（SHA-256 不变）的前提下，`SessionStart` 与 `UserPromptSubmit` 作为零状态被动直通入口运行；`Stop` 优先校验社区分享授权与 `project_roots` 范围，未开启或越界场景 `< 100ms` 零 SQLite 退出，已开启且在授权目录内时自动补建 `not_before=0.0` 会话租约并执行脱敏轨迹采集。
- **纯语义隐式 `SKILL.md` 与文档同步**：移除 `disable-model-invocation: true` 并开启 `openai.yaml` 的 `allow_implicit_invocation: true`，重写精简 `<= 30` 行昇腾 NPU / vLLM-Ascend / xLLM / MindIE-SD / AscendC / Triton 运行时工作流说明，清理全部面向用户的手动激活前缀提示。

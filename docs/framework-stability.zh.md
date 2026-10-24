# 框架稳定性与持久化配置

<b>中文</b> | <a href="framework-stability.md">English</a> · <a href="README.md">返回文档索引</a>

本文说明 `mindie-agent-codex` 的日常无感使用契约、Windows 原子配置读写机制，以及组件级测试复现流程。

---

## 1. 日常使用与配置持久化

- **授权一次配置，知识按会话显式启用**：社区知识贡献与自动工具故障上报共用 `<profile>/.mindie/mindie-consent.json` 中的统一贡献开关。保存后的统一贡献选择与已批准项目范围跨任务、Fork 分支、宿主重启、插件升级与普通临时故障持久保持，无需重复确认授权。确定性工具（`mindie-remote-dev` 与事实状态查询 `bridge status`）在所有会话中默认可用；而知识与经验飞轮（`knowledge_query`、`knowledge_explain`、`knowledge_feedback`、`knowledge_distill`、`knowledge_sync` 及 `Stop` 后台采集）则通过在当前会话中显式输入一次 `/mindie-agent`（或 `$mindie-agent`）按需启用。若旧版仅知识 `contribute` 配置缺少统一授权版本（`choice_revision`），升级后不会自动开启故障上传，而是保留原状态并显示 `needs_choice`，待用户显式确认一次后再启用。
- **未激活会话或关闭贡献时零额外开销**：在未调用 `/mindie-agent`（或 `$mindie-agent`）的普通会话中，或当社区贡献处于关闭状态时，`Stop` 阶段不执行任何对话读取或整理模型调用；远端昇腾开发（`mindie-remote-dev`）默认常驻可用，领域知识检索（`mindie-knowledge`）可通过 `/mindie-agent` 随时唤醒。
- **损坏配置保护**：临时网络或服务波动由后台 Worker 自动恢复；若配置文件损坏，系统直接作为显式故障报告，绝不静默重置为首次安装状态。

---

## 2. Windows 平台原子配置读写

- **删除共享读取 + 原生原子重命名**：共享配置存储结合了删除共享（Delete-Sharing）读取与 Windows 原生原子重命名语义（[`FILE_RENAME_INFORMATION`](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information)），确保并发读取者要么读到完整的旧配置，要么读到完整的新配置。
- **外部占用安全保护**：若外部程序以未开启删除共享的方式占用配置文件，写入操作会保留已保存的原配置并显式报错，不会触发死循环重试或配置重置。

---

## 3. 可复现的组件测试与基准测试

组件测试使用仓库内提交的真实解析器夹具与 `plugins/mindie-agent/pyproject.toml` / `uv.lock` 中锁定的精确组件提交，不探测或修改用户本机的生产环境安装：

```sh
export MINDIE_CORE_REPO=/path/to/knowledge
export MINDIE_KIMI_REPO=/path/to/mindie-agent-kimi
uv sync --project plugins/mindie-agent --locked --no-editable
uv run --project plugins/mindie-agent --no-sync python tests/run_ci.py
```

- **随版 CPU 微基准测试（Release Microbenchmark）**：每个 `0.0.2` 发行版均运行 [`benchmarks/release.json`](../benchmarks/release.json)（[`benchmarks/release.py`](../benchmarks/release.py)）测量 `public-transcript` 公开消息解析与隐藏推理过滤的延迟（`us/op`），并随 GitHub Release 附带 `benchmark-results.json` 和 `benchmark-results.md` 作为可复现的本地 CPU 性能基线。
- **配套发布顺序**：在更新共享运行时依赖时，先发布底层组件正式 Release，再执行各宿主插件适配器的验证与发布。
- **清晰的验收边界**：自动化 CI 与 CPU 微基准负责验证锁定依赖、解析器夹具与跨平台文件系统契约；原生宿主会话闭环与远端昇腾 NPU 硬件执行作为独立验收项管理。

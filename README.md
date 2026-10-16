<p align="center">
  <img src="https://raw.githubusercontent.com/mindie-agent/mindie-agent/main/assets/brand/banner.svg" alt="MindIE Agent Codex Plugin" width="900">
</p>

# MindIE Agent · OpenAI Codex CLI 插件

中文 | [English](README.en.md)

**面向昇腾（Ascend）推理工程的 OpenAI Codex CLI 原生插件：领域知识检索、本地任务经验闭环与 SSH 远端 NPU 开发底座**

`mindie-agent-codex` 是 [MindIE Agent](https://github.com/mindie-agent/mindie-agent) 的 OpenAI Codex CLI 原生插件（首发版本 `v0.0.1`，当前聚焦 `vLLM` / `vLLM-Ascend` 领域），完整遵循 [MindIE Agent 九条设计原则](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.md)。

插件在你的业务仓库与原生 Codex 会话中直接工作：提供 1 个信息型入口 Skill、3 个领域知识工具（检索、分块读取、独立有用性反馈）以及完整的 `remote-dev` SSH 远端执行工具集。

> 阅读 [用户须知 / Contribution Notice](plugins/mindie-agent/skills/mindie-agent/references/user-notice.md) 了解公开消息筛选范围、确定性脱敏边界、模型处理与公开 Git 历史说明。如需搭建 `vllm` + `vllm-ascend` 本地多任务 Git Worktree 工作区，可按需参考 [vllm-ascend-workspace](https://github.com/mindie-agent/vllm-ascend-workspace) 与 [工作区配置说明](plugins/mindie-agent/skills/mindie-agent/references/workspace-setup.md)。

---

## 核心特性

- **原生无感接入**：在现有业务仓库中正常发起 Codex 会话即可按需调用知识库与远端 NPU 工具，无需每次任务手动激活或执行收尾命令。
- **双层领域知识与经验闭环（[`mindie-knowledge`](https://github.com/mindie-agent/knowledge)）**：
  - 自动同步 [`knowledge-vllm-ascend`](https://github.com/mindie-agent/knowledge-vllm-ascend) 公共工程知识包，提供本地 BM25 与分块引用检索。
  - 在用户显式开启社区贡献后，通过确定性规则与 Gitleaks 8.30.1 筛选并脱敏公开对话，调用原生小模型生成分块导航摘要并自动提交 PR。
- **SSH 远端昇腾开发（[`mindie-remote-dev`](https://github.com/mindie-agent/remote-dev)）**：提供编辑器语义的远端文件读写、补丁应用、受管后台作业、日志追踪与产物同步，独立于经验采集开关。
- **有界本地诊断与可选故障上报（[`diagnostics`](https://github.com/mindie-agent/diagnostics)）**：内置有界结构化诊断日志，支持独立于知识贡献的显式授权工具故障 Issue 上报。

---

## 环境要求与支持矩阵（`v0.0.1`）

- **基础依赖**：`git`、`uv` 以及支持原生插件机制的已登录 `codex` CLI。
  - **原生 Windows**：由 Git for Windows 提供受支持的 SSH 客户端。
  - **Linux / Windows WSL**：须在 WSL 内部使用 Linux 原生 `codex`、`git` 与 `uv`（配置、SQLite 与运行时均位于 Linux 文件系统，切勿跨 `/mnt/c` 混用 Windows 可执行文件）；后台知识服务依赖可达的 `systemd --user`（版本 `>= 254` 且启用统一 cgroup v2 层级，可通过 `systemctl --user show --property=Version` 核对）。
- **已验证宿主版本区间**（声明于 [`harness-compatibility.json`](plugins/mindie-agent/harness-compatibility.json)）：
  - macOS：`codex-cli =0.162.0-alpha.17.2`
  - 原生 Windows / Windows WSL：`codex-cli =0.153.4`

---

## 快速开始（安装与验证）

在 macOS / WSL 终端或原生 Windows PowerShell 中，使用同一 Codex Profile 执行以下命令安装不可变的 `v0.0.1` 正式版：

```sh
git --version
uv --version
codex --version
codex plugin marketplace add https://github.com/mindie-agent/mindie-agent-codex.git --ref v0.0.1
codex plugin add mindie-agent@mindie-agent --json
codex plugin list --json
```

1. 确认 `codex plugin list --json` 显示 `mindie-agent@mindie-agent` 版本为 `0.0.1` 且处于启用状态。
2. 启动一个新的原生 Codex 会话，触发 `SessionStart` 与 MCP 初始化（首次运行将通过 `uv sync --locked --no-editable` 在 `PLUGIN_DATA` 下构建隔离的锁定运行时，首次准备窗口最长为 10 分钟）。
3. 确认原生工具发现同时暴露出 `mindie-knowledge` 与 `mindie-remote-dev`。

---

## 日常使用与可选管理命令

日常开发无需额外命令。当需要显式查看状态、调整共享授权、导入历史会话或管理故障上报时，可通过固定发行版入口 `entry.py` 调用 `bridge` 子命令：

| 操作场景 | 命令示例 | 说明 |
|---|---|---|
| **查看运行状态** | `python3 <PLUGIN_DATA>/releases/0.0.1/entry.py bridge status` | 只读查询插件绑定、知识服务、采集游标与同步状态 |
| **配置/修改共享范围** | `python3 <PLUGIN_DATA>/releases/0.0.1/entry.py bridge configure` | 配置或更新社区贡献授权与项目范围 |
| **关闭社区贡献** | `python3 <PLUGIN_DATA>/releases/0.0.1/entry.py bridge sharing-disable` | 立即停止当前范围的新增采集与外发 |
| **手动刷新公共知识库** | `python3 <PLUGIN_DATA>/releases/0.0.1/entry.py bridge sync` | 执行一次无模型公共知识源增量同步 |
| **显式导入历史会话** | `python3 <PLUGIN_DATA>/releases/0.0.1/entry.py bridge history-import --source /path/to/rollout.jsonl` | 显式导入选定的历史 JSONL 会话（详见 [历史会话导入](docs/history-import.zh.md)） |
| **管理故障自动上报** | `python3 <PLUGIN_DATA>/releases/0.0.1/entry.py bridge reporting-status` | 查询或切换（`reporting-enable` / `reporting-disable`）工具故障上报开关 |

### 停止使用与卸载

关闭正在使用该插件的会话并退役其知识服务后，通过 Codex 原生插件命令卸载：

```sh
codex plugin remove mindie-agent@mindie-agent --json
```

---

## 静默更新与已知限制

- **静默兼容更新**：距上次成功检查超过 24 小时后，下一次自然 MCP 调用会在后台异步检查当前 Codex 版本兼容的最高正式 Release，在锁外完成准备后于服务空闲时原子切换，保留已保存的贡献授权与故障上报设置，无需手动重载。
- **已知宿主限制（子代理中断）**：在 `0.0.1` 中，当父回合中断正在等待工具调用的子代理（Child Agent）时，Codex 宿主可能未向该子代理的在途调用送达取消信号，导致本地辅助进程/SSH 或已启动的远端命令继续运行。详见 [Harness 接口证据与已知限制](docs/harness-interfaces.md#known-limitation-child-agent-cancellation)（[中文说明](docs/harness-interfaces.zh.md#known-limitation-child-agent-cancellation)）。持久远端后台作业仍通过 `remote-dev` 独立生命周期控制。

---

## 文档导航

完整文档索引见 [docs/README.md](docs/README.md)（[English](docs/README.en.md)）：

| 中文文档 | English | 说明 |
|---|---|---|
| [Codex Harness 接口兼容性证据](docs/harness-interfaces.zh.md) | [Harness Interface Evidence](docs/harness-interfaces.md) | `harness-compatibility.json` 登记接口、原生管理、主线程中断与子代理取消已知限制 |
| [公开对话采集与脱敏](docs/public-transcript.zh.md) | [Public Transcript Capture](docs/public-transcript.md) | 原生 `response_item` 筛选、Gitleaks 8.30.1 增量扫描与不可变分块提交 |
| [显式历史会话导入](docs/history-import.zh.md) | [Explicit Historical Contribution](docs/history-import.md) | 显式指定 JSONL 历史会话导入的权限校验与增量脱敏流程 |
| [已安装服务身份与连续性](docs/service-continuity.zh.md) | [Installed Service Identity](docs/service-continuity.md) | 脱离易失缓存的锁定运行时、单一配置绑定与空闲切换契约 |
| [故障诊断与取消边界](docs/failure-diagnostics.zh.md) | [Failure Diagnostics](docs/failure-diagnostics.md) | 只读状态诊断、本地进程组取消清理与远端持久作业分离 |
| [框架稳定性与持久化配置](docs/framework-stability.zh.md) | [Framework Stability](docs/framework-stability.md) | 日常使用持久选择、Windows 原子配置读写与验收边界 |
| [测试边界与契约校验](docs/testing-contracts.zh.md) | [Test Boundaries & Contracts](docs/testing-contracts.md) | `tests/preflight.py` 预检、固定依赖提交校验与多维契约测试矩阵 |

---

## 开发者测试

```sh
export MINDIE_CORE_REPO=/path/to/knowledge-checkout
export MINDIE_KIMI_REPO=/path/to/kimi-adapter-checkout
uv run --project plugins/mindie-agent --no-sync python -m unittest discover -s tests -p 'test_*.py'
```

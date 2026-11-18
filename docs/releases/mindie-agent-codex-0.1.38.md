# MindIE Agent Codex v0.1.38 — Great Subtraction & UX Upgrade

## 概要

`v0.1.38` 是一次结构性减法与安装体验升级：把 Codex 插件从 `17,734` 行自维护运行时代码压缩到 **`< 1,200` 行**（本发布为 `1,099` 行），把知识、远端、诊断、MCP、锁与运行时构建全部交给 `mindie-harness`（`API_VERSION = 1`）及共享组件，同时保持既有 Hook 信任哈希、`codex.json` 布局与升级连续性不变。

## Great Subtraction

- **删除重复运行时**：移除 `bridge.py`、`mcp_gate.py`、`service_control.py`、`storage_gc.py`、`release_activation.py`、`release_migration.py`、`harness_update.py`、`component_update.py`、`agent_worker.py` 等 50+ 个历史模块及其内部测试。
- **保留 10 个薄文件**：`__init__.py`、`with_runtime.py`、`mcp_entry.py`、`hook_launcher.py`、`bootstrap.py`、`entry.py`、`host_adapter.py`、`native_hooks.py`、`codex_transcript.py`、`setup.py`。
- **单一 MCP 契约**：`remote-mcp`、`tools-mcp`、`knowledge-mcp` 全部交给 `mindie_harness.api.serve_mcp` 与 `ManagedMCPAdapter`，原生 `mindie_interrupt` 取消由共享 `MCPStdioServer` 统一处理。
- **共享组件轮子仓**：`setup.py update` 委托 `mindie_harness.runtime.wheelhouse.update_runtime`，使用预构建 wheelhouse，校验通过后空闲原子切换。
- **升级连续性**：`bootstrap.py` 在 `0.1.23 -> 0.1.38` 升级中读取旧解释器的组件清单，若旧组件 SemVer 严格更新，则按精确 `commit_id` 重新安装，绝不降级独立升级过的组件下限。
- **Hook 清单字节冻结**：`hooks/hooks.json` 保持 `25,650` 字节与 `sha256 == ea80d0815485ba5f762ff5275f1e40817130694870405237dbd65d03f28f9981` 不变。

## 性能与快速路径

- `UserPromptSubmit`：< 15ms 输出 `{}`，不读取 stdin。
- 未配置、`disabled`、`read-only` 或 `cwd` 不在 `project_roots` 内的 `Stop`：< 30ms 输出 `{}`，零 SQLite、零 `mindie_harness` 导入。
- `SessionStart`：已有活动解释器时仅原子写入 `pending-update-check.json`，不启动构建。
- `/work/app-other` 与 `/work/app` 的 `commonpath` 前缀陷阱在 POSIX 与 Windows 同盘比较下均不会误判。

## UX 升级

- **确定性安装**：`uv run https://github.com/mindie-agent/mindie-agent/releases/latest/download/install.py --host codex` 一次完成插件安装、锁定运行时预热与配置初始化。
- **`gh` 可选**：`gh` 已认证时推荐 `contribute`；缺失/未登录/不可达时推荐 `local-only`，零 GitHub 上传且无需 `gh`，安装与远端 NPU 能力不受影响。
- **3 问动态 Onboarding**：Codex 原生 `request_user_input` 依次询问工作区、经验与隐私模式（`contribute` / `local-only` / `read-only`）以及可选的一步 SSH NPU 接入。
- **只读 `mindie_doctor`**：展示运行时、配置、共享模式、SSH/NPU 就绪卡片；失败只报告具体阶段，不阻塞工程任务。
- **10 工具 `codex` remote-dev profile**：`remote_exec_command`、`remote_write_stdin`、`remote_view_image`、`remote_apply_patch`、`remote_auth`、`remote_probe`、`remote_artifact_pull`、`remote_artifact_push`、`remote_job_status`、`remote_job_stop`。
- **礼貌 Star 附言**：仅在用户明确同意后执行 Star，绝不静默操作。

## 兼容性

- Codex CLI：精确 `0.153.4`，以及 `>=0.162.0-alpha.2` 且 `<=0.999.999`（含预发布）。
- `mindie-harness API_VERSION = 1`；`mindie-harness-seed.pyz` 已更新至 `v0.1.3`。
- Python `>= 3.11`；macOS、原生 Windows、Linux / WSL 均可运行。
- 现有 `codex.json`、`codex.engine.json`、`releases/<version>/runtime.json` 与已保存共享选择保持兼容。

## 验证

```sh
python3 .github/scripts/check_lf_eol.py
pytest -q -m "not live_npu" tests
python3 plugins/mindie-agent/scripts/setup.py release-metadata --source plugins/mindie-agent --tests tests --revision "$(git rev-parse HEAD)"
```

发布契约测试覆盖：Hook 清单 SHA-256、脚本行数上限、seed 校验与 `HostAdapter` 类型、`.mcp.json` 定位器、`release-metadata`、停止/提示快速路径、`migrate` 重绑定、MCP 面隔离与 `mindie_interrupt`、`NativeHooks` CAS、transcript 公共消息与 fork 边界。

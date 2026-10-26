# Codex Harness 接口兼容性与验证证据

<b>中文</b> | <a href="harness-interfaces.md">English</a> · <a href="README.md">返回文档索引</a>

[`plugins/mindie-agent/harness-compatibility.json`](../plugins/mindie-agent/harness-compatibility.json) 是 `mindie-agent-codex` 所使用的 OpenAI Codex CLI 宿主接口的机器可读注册表。当前 `v0.0.2` 发行版声明支持两个经实机验证的 Codex 版本：
- **`0.153.4`**（对应官方上游源码提交 `3d2ee51ca2d5db578f328aa75e20aa22c0197c9a`）
- **`0.162.0-alpha.17.2`**（对应官方上游源码提交 `740e5af33c71225640e0c1c1555c514c2c93ab74`）

在 CI 流程中，`plugins/mindie-agent/scripts/release_metadata.py` 会自动核对每个已登记的生产代码符号与契约测试方法，并校验所有使用接口与声明版本区间的一致性。

---

## Native management {#native-management}

上述两个 Codex 版本均在 `codex-rs/app-server-protocol/src/protocol/common.rs` 中声明了 `hooks/list`、`config/read` 与 `config/value/write` 接口，支持读取并通过乐观并发控制（CAS `expectedVersion`）更新 Hook 的启用状态与信任哈希（`hooks.state.<key>.trusted_hash`）。

### 原生插件市场与 Hook 信任继承机制
- **固定不可变 Release Tag**：通过 `--ref v0.0.2` 注册插件市场可将目录锁定在不可变发行标签上，防止后续 `main` 分支演进时宿主在未经兼容性检查的情况下自动拉取未验证版本。
- **Git 子目录插件源**：原生目录项采用 `source: "git-subdir"`（指定精确的 `url`、`path` 与 `sha`），已通过原生 `codex plugin list` 与 `codex plugin add` 实机验证。
- **严格尊重用户 Hook 授权**：在静默兼容更新时，`native_hooks.py` 仅通过原生配置 CAS 更新本插件源至目标 SHA，并**仅对用户此前已启用且信任的本插件 `SessionStart`、`UserPromptSubmit`、`Stop` 与 `Interrupt` Hook** 继承信任哈希；用户显式禁用或撤权的 Hook 保持禁用，且绝不触碰其他无关插件。

---

## Native turn interruption {#native-turn-interruption}

尽管 [MCP 协议规范](https://modelcontextprotocol.io/specification/2025-06-18/basic/utilities/cancellation)将 `notifications/cancelled` 定义为可选通知，但在实机验证中发现：当 Codex `0.153.4` 执行回合中断（`turn/interrupt`）时，宿主并未向已通过审批、正在执行中的 MCP `tools/call` 请求转发标准取消通知（参见上游 [`mcp_tool_call.rs`](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/mcp_tool_call.rs) 与 [`rmcp_client.rs`](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/rmcp-client/src/rmcp_client.rs)）。

为了在用户打断主线程回合时立即回收本地辅助进程与 SSH 子进程树——同时不引入轮询监工进程或武断的业务超时——`mindie-agent-codex` 在支持标准 MCP 取消通知与 `EOF` 清理的基础上，通过下述原生 `Interrupt` Hook 补齐了主线程取消链路。

---

## Native Interrupt Hook {#native-interrupt-hook}

插件为 `mindie-knowledge` 与 `mindie-remote-dev` 两个 MCP 服务声明了 `mcp_tool` 类型的原生 `Interrupt` Hook：
- **精准定位在途调用**：当主线程回合被用户打断时，宿主通过该 Hook 在同一条 MCP 连接上调用内部控制入口 `mindie_interrupt`（传入原生 `${session_id}` 与 `${turn_id}`，该控制入口不对大模型暴露）。
- **绕过工作队列阻塞**：控制请求在 MCP 读取层（进入 Worker 容量门限之前）直接执行，核对 `_meta.threadId` 后仅向线程与回合 ID 完全匹配的在途调用发送取消事件。
- **毫秒级本地进程树回收**：在 macOS、原生 Windows 与 WSL Ubuntu 24.04 三大平台（Codex `0.153.4` 锁定非 editable 安装包）实机验证中，打断挂起的 SSH 工具调用后均在百毫秒内干净回收全部本地辅助与 SSH 子进程，且 MCP 服务与宿主会话保持健康响应：

| 运行平台 | 验证 Codex 版本 | 中断后本地进程树清理耗时 | 回收归属进程数 |
|---|---|---|---|
| macOS | `0.153.4` | 76.41 ms | 6 |
| 原生 Windows | `0.153.4` | 113.82 ms | 5 |
| WSL Ubuntu 24.04 | `0.153.4` | 65.81 ms | 6 |

- **与远端持久作业解耦**：回收本地 SSH 传输子进程不代表远端已启动的命令自动回滚；通过 `remote-dev` 创建的远端持久后台作业拥有独立的作业 ID 与生命周期。

---

## Known limitation: child-agent cancellation {#known-limitation-child-agent-cancellation}

在 `0.0.2` 中，Codex 宿主的原生 `Interrupt` 事件仅针对主线程触发，其上游实现显式跳过了通过 `spawn_agent` 创建的子代理（SubAgent）；同时 `SubagentStop` 仅在正常完成时触发，不会在 `TurnAborted` 路径下分派。

- **实际影响**：当子代理正处于等待 MCP 工具调用返回状态而被打断时，该在途调用既收不到 `notifications/cancelled`，也收不到 `Interrupt` Hook 信号，导致子代理发起的本地辅助进程/SSH 或已启动的远端命令可能继续运行直至宿主进程退出。
- **工程处理原则**：遵循 [MindIE Agent 工程设计原则](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.md)，插件如实向用户披露这一上游宿主限制，而不私自分发魔改宿主二进制、后台轮询杀进程或设置武断的业务超时。上游问题追踪与实机复现证据已同步至 [openai/codex#26956](https://github.com/openai/codex/issues/26956)。

---

## 运行时与归属边界

- **宿主与运行时解耦**：Codex 宿主管理插件安装记录与缓存目录；`uv` 在宿主易失缓存之外构建不可变的锁定 Python 环境；适配器配置文件作为唯一权威绑定指向当前生效的完整运行时。


## Codex 0.162.1 / 0.164.0 稳定版验收（2026-10-26）

兼容声明新增两个精确版本：本机 `0.162.1` 与当日官方最新稳定版 `0.164.0`。原有支持项保留，未验证的补丁版和预发布版本仍不放行。

两个版本均在 WSL Linux 使用真实可执行程序通过 `tests/test_native_harness_acceptance.py`：插件安装与回读、五个 Hook 的发现、配置 CAS、已信任 Hook 的继承与显式禁用的保留、锁定运行时构建、原生 SessionStart，以及 22 个远端工具和 5 个组件工具的 MCP 发现。普通会话保持知识系统未启用。Ubuntu CI 对这两个版本执行同一无模型验收；本次证据不包含付费模型轮次、远端 NPU 或新增版本在 Windows/macOS 的实机验收。

两个官方版本的 `mcp_tool_call.rs` 均保留插件依赖的原生线程与轮次元数据；现有协议测试继续验证身份拒绝与按轮次取消行为。复现方式：设置 `MINDIE_TEST_CODEX_BIN` 为目标 Codex 的绝对路径，并在锁定测试环境中运行上述测试文件。

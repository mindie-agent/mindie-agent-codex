# Codex Harness 接口兼容性证据

中文 | [English](harness-interfaces.md) · [返回文档索引](README.md)

机器可读的兼容性注册表位于 [`plugins/mindie-agent/harness-compatibility.json`](../plugins/mindie-agent/harness-compatibility.json)，声明支持两个经实机验证的 Codex 版本单点：`0.153.4`（对应官方源码提交 `3d2ee51ca2d5db578f328aa75e20aa22c0197c9a`）与 `0.162.0-alpha.17.2`（对应官方源码提交 `740e5af33c71225640e0c1c1555c514c2c93ab74`）。完整英文实机证据详见 [`harness-interfaces.md`](harness-interfaces.md)。

## Native management {#native-management}

两个版本均在 `codex-rs/app-server-protocol/src/protocol/common.rs` 中声明 `hooks/list`、`config/read` 与 `config/value/write`，支持读取与乐观并发更新（CAS `expectedVersion`）Hook 的启用与信任哈希状态（`hooks.state.<key>.trusted_hash`）。静默更新仅继承用户已对本插件启用并信任的 `SessionStart` / `Stop` Hook，绝不绕过用户显式撤权或修改其他插件。

## Native interrupt hook {#native-interrupt-hook}

在主线程回合被中断时，宿主通过内部 `mcp_tool` Interrupt 通道向同一在途 MCP 连接发送带精确 `thread` / `turn` 身份的取消控制事件；插件在约 73 毫秒内精准回收该调用拥有的本地辅助子进程树，而 MCP 监听进程与宿主会话保持健康响应。

## Native turn interruption {#native-turn-interruption}

主线程工具调用取消、EOF 进程树清理与 `remote-dev` 持久远端作业生命周期保持清晰分离：打断当前回合只会回收本次前台工具调用拥有的本地辅助进程树，不会误杀已独立托管的远端持久作业。

## Known limitation: child-agent cancellation {#known-limitation-child-agent-cancellation}

在 `0.0.1` 中，当通过原生 `spawn_agent` 创建的子代理（Child Agent）正处于等待 MCP 工具调用返回状态而被打断时，Codex 宿主源码中的 Interrupt 分派排除了 SubAgent（且 `SubagentStop` 仅在正常完成时触发），导致标准 MCP 取消信号或管道 EOF 未送达该在途子调用，本地辅助进程/SSH 或已启动的远端命令可能继续运行直至宿主进程退出。该宿主限制已在 `0.0.1` 中明确披露并接受，插件不引入轮询猎杀进程或武断业务超时等破坏边界的补偿机制。

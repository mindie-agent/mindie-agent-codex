# mindie-agent-codex 0.1.30：Windows 手动唤醒链路、错误可见性与分发状态诚实修复

[English](mindie-agent-codex-0.1.30.en.md)

修复 Windows PowerShell 5.1 无控制台后台执行（`CREATE_NO_WINDOW`）设置 `[Console]::OutputEncoding` 抛出 `IOException`、`\\?\` 扩展路径别名导致 `hook_launcher.py` / `entry.py` / `release_activation.py` 解释器与配置比对失配、受限宿主 Job Object 禁止 `BREAKAWAY_OK` 时进程启动失败，以及 Codex Desktop/TUI 链接式提及（`[$mindie-agent](...)` / `[@mindie-agent](plugin://...)`）与 UTF-8 BOM 的显式唤醒识别（mindie-agent#335）。普通非 `/mindie-agent` 提示词严格保持零上下文与零落盘（P2.1）。

`UserPromptSubmit` 与 `bridge.py activate` / `knowledge_cli.py` / `runtime_call.py` 不再把 Hook 执行失败、只读沙箱写入受阻、会话 `CODEX_THREAD_ID` 失配或未送达的 Hook 笼统归结为要求用户循环重发 `/mindie-agent`；系统按具体阶段给出脱敏诊断，并引导继续执行确定性工具与原生主机任务。

修复 `entry.py` 将业务分发包入 `profile-binding` 阶段及损坏配置 JSON 触发 `TypeError`（mindie-agent#341）、后台维护非阻塞锁竞争 `BlockingIOError` 误记故障（mindie-agent#339）、目录 JSON 解析异常分类，以及 `runtime_call.py` 在 RPC 前或只读查询上误标 `execution=outcome_unconfirmed` 与隐藏 `ServiceHostError` 阶段码的问题（mindie-agent-codex#85、mindie-agent#320、mindie-agent#321）。

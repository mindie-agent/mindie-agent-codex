# 自然语言 RSI 执行说明

自然语言 RSI 任务通过 `mindie-harness` 的 `rsi_plan` 和 `rsi_autotune` 工具生成并执行
经过校验的计划。`rsi_autotune` 复用 `experiment_ledger` 的候选门禁和证据记录接口，
并把可用的源指纹、候选条件和进度写入同一份 ledger。`rsi_loop` 保留为交互式
`profile -> consult -> propose -> measure -> learn` 流程的可选工具。Codex 插件不复制
客户机运行时。

## 计划输入

- 基线服务的启动脚本或启动命令；
- benchmark、correctness 或 accuracy 测试定义；
- 优化目标，例如吞吐、TTFT、延迟、显存或接受 token 数；
- 主机、源码目录、设备、端口和运行时绑定。

缺少必要信息时返回 `needs_input`，不得猜测命令、路径、模型、数据集或测量结果。

## 执行路径

1. 读取源码、启动脚本、测试定义、设备和运行时绑定，确认工作目录在用户授权范围内；
2. 使用 `knowledge_query` 和 `knowledge_explain` 补齐目标所需的知识依据；
3. 调用 `rsi_plan` 的 `validate` 或 `render`，确认输入完整、路径受限、候选范围有界；
4. 使用 `coordinator` 获取独占设备租约，检查已有 `execution_id` 和 `job_id` 后再启动运行；
5. 调用 `rsi_autotune` 执行 `prepare -> build -> accuracy -> performance -> cleanup` 候选波次；
6. 需要交互式知识循环时，使用 `mindie_harness.rsi.rsi_loop` 维护 `profile -> consult -> propose -> measure -> learn` 状态；
7. `rsi_autotune` 使用 `mindie_harness.rsi.experiment_ledger` 的 gate 和 append 接口记录 fingerprint、候选、测量和 verdict；
8. 只有 correctness、benchmark、重复测量和用户指定门禁全部通过，才更新 baseline。

`rsi_plan`、`rsi_autotune`、`rsi_loop` 和 `experiment_ledger` 都由 `mindie-tools` 的组件面提供。
其中 `rsi_autotune` 负责候选队列和断点恢复，并复用 `experiment_ledger` 的账本锁、证据门禁、
重复候选记录和 `unknown` 状态处理。`rsi_loop` 负责另一种交互式状态机。
插件侧只负责把自然语言目标转换为结构化参数，并把用户确认的步骤转发给这些工具。

## 安全边界

- 客户运行时命令使用 argv 数组或工具参数，远端步骤只调用固定入口并使用经过校验的路径；
- 启动脚本和测试定义是寻优边界，默认只调整其中声明的运行时参数；
- 默认不修改源码、不生成仓库外算法，也不把缺失证据标记为通过；
- 超时、连接中断或外部结果未知时停止当前候选，保留句柄并先完成状态核对；
- 物理 NPU 测量前必须持有 coordinator 租约，并使用显式 profiler 时间窗口。

## 计划字段

自然语言层生成的计划至少包含 `schema`、`task_id`、`endpoint`、`source_root`、`rsi_root`、
`startup_script`、测试定义、`baseline`、`objective` 和 `operations`。`source_root`、`rsi_root`
和脚本或配置路径必须是无 `..` 的绝对远端路径，测试命令必须是 argv 数组或无 shell 元字符的
命令字符串。`operations` 只允许计划 schema 声明的操作。

`rsi_autotune` 支持显式候选或有界笛卡尔候选空间，默认最多 256 个候选。只有所有必需门禁和
配对速度下界通过后才写入 `active-best.json`。计划只描述意图和边界，具体的 profile、candidate、
measure 和 learn receipt 由 harness 生成，不得在插件内维护第二份状态机或 `active-best` 文件。

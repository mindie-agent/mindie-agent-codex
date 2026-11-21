# 自然语言 RSI 执行说明

RSI 将用户的自然语言性能目标转换为经过校验的 `mindie-agent/rsi-plan/v1` 计划，并在客户提供的启动脚本和测试定义上执行受控的参数寻优。

## 用户需要提供的信息

- 可复现基线服务的启动脚本或启动命令；
- 性能测试脚本、命令、配置文件，或清晰的测试描述；
- 优化目标，例如端到端吞吐、TTFT、延迟、显存或接受 token 数；
- 主机、源码路径、设备、运行时和端口绑定。

缺少必要信息时返回 `needs_input`，系统不会编造命令、路径或测试结果。

## 执行流程

1. 只读检查源码、RSI 目录、启动脚本、测试定义、设备和运行时绑定；
2. 校验计划并申请 coordinator NPU 租约；
3. 通过 runtime adapter 执行 `prepare`、`build`、`profile`、`accuracy`、`performance` 和 `cleanup`；
4. 每个动作必须返回 JSON receipt；失败或未知状态停止当前候选并执行清理；
5. 在相同 workload、seed、输入输出长度和 batch 配置下比较候选与 baseline；
6. 只有 benchmark、correctness、重复测量和用户指定的性能门禁通过，才更新 `active-best`。

所有命令使用 argv 数组执行，不经过 shell。客户的启动脚本和测试命令是寻优边界，RSI 只调整其中声明的运行时参数。

## 参数寻优范围

默认候选顺序为：

1. 启动脚本中的运行时参数；
2. 仓库和当前运行时已经存在的可选特性。

默认从启动脚本发现 `num_speculative_tokens`，测试当前值附近的步进候选；也支持用户在 `candidate_space` 中显式给出参数笛卡尔积。每个候选都有独立状态、设备/端口绑定和结果 receipt，支持串行或多 worker 并行执行。

本流程不生成新的论文算法、不检索或实现仓库外算法、不修改源码。源码目录只作为候选构建和运行时检查输入；源码 patch/implementation operation 不属于默认计划。

## 测量和证据

workload manifest 记录启动脚本、参数、数据集、模型版本、seed、预热次数、重复次数、prompt ID 和命令哈希。benchmark 记录端到端耗时、输入输出 token、TTFT、解码速率、batch 和并发信息；profiler 记录 MindIE Agent profiler 返回的阶段和 kernel 数据。

性能比较使用配对观测和置信区间。任务正确性使用用户配置的数据集或测试命令；只有用户明确要求 lossless token-level 校验时才启用 teacher-forcing scorer。

缺少 profiler、benchmark 或 correctness receipt 时，候选状态为 `unavailable` 或 `failed`，不会晋升 active-best。

## 运行时绑定

`rsi_install.py` 幂等创建标准入口和绑定模板。绑定文件提供主机、源码路径、设备、端口、runtime entry、profile、benchmark、accuracy 和 cleanup 命令，以及 Python、环境变量和超时配置。

不同客户只需复制绑定模板，填写自己的主机和启动脚本参数；寻优控制器和计划格式保持不变。

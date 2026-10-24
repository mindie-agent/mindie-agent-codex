# mindie-agent-codex 0.1.7

普通 Codex 会话的 MCP 发现结果不再包含 knowledge 工具名称、描述或参数 schema。显式调用 `/mindie-agent` 后，Skill 通过当前会话授权保护的私有入口加载 schema 并访问原有知识引擎；其他聊天的授权不会改变共享工具目录。

新增 `mindie-tools` MCP，提供 profiler、sim、coordinator 与本地 diagnostics 状态和诊断包导出。前三个组件复用各自独立安装的正式 CLI，可通过 `component_commands` 指定解释器；缺少安装时返回明确错误。remote-dev 保持默认可用，面向人的 npu-top 不注册为 Agent 工具。

组件执行复用现有超时、取消和进程树清理，限制输出大小并保留退出码及原始输出。本地合同覆盖实际 MCP 发现、跨聊天隔离、私有知识查询、组件 CLI 成功与失败。发布流水线另附精确 Tag 源码的合同测试与公开消息解析微基准；这些结果不代表 NPU 性能或所有客户端的自动更新验收。

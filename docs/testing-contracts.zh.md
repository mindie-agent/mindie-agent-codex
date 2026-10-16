# 测试边界与契约校验

中文 | [English](testing-contracts.md) · [返回文档索引](README.md)

本文定义 `mindie-agent-codex` 的预检脚本（`tests/preflight.py`）与契约测试边界。完整英文细则见 [`testing-contracts.md`](testing-contracts.md)。

## 契约测试维度

| 测试边界 | 验证内容 | 不替代的边界 |
|---|---|---|
| **锁定依赖探针** | 校验 `uv.lock` 中固定的运行时组件提交与接口签名 | 真实原生模型行为 |
| **发布兼容性** | 验证运行时生成的知识包通过 `publication-contract.json` 指定的独立校验器 | 远端 Bot 真实授权与合入 |
| **Windows 进程归属** | 验证真实子进程、继承管道、主进程退出与外部看门狗清理 | POSIX 进程组行为 |
| **服务交接与 UTF-8** | 验证认证空闲确认、消费锁释放、重启交接以及含中文/Unicode 路径下的完整回合 | 实机 NPU 硬件执行 |

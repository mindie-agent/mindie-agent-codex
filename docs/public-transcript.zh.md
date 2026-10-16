# 公开对话采集与脱敏

中文 | [English](public-transcript.md) · [返回文档索引](README.md)

本文说明 `mindie-agent-codex` 如何从原生 Codex JSONL 会话记录中提取公开对话、执行本地脱敏并落盘为不可变分块。完整英文细则见 [`public-transcript.md`](public-transcript.md)。

## 1. 确定性公开消息筛选

- 读取器仅保留原生 `response_item` 中的用户输入、助手公开进展说明与最终回答。
- 工具调用与返回结果、隐藏推理（Reasoning）、系统/开发者提示词、注入目录以及从父任务继承的 Fork 历史均被确定性剔除。
- 缺失时间戳、损坏记录或覆盖不完整均显式报错，绝不静默推进采集游标。

## 2. Gitleaks 8.30.1 与隐私规则脱敏

- 本地强制运行固定版本 Gitleaks `8.30.1` 与确定性隐私规则，忽略源文件中的 `gitleaks:allow` 注释。
- 跨分页扫描仅传递未闭合私钥状态，绝不在状态中缓存明文密钥。
- 脱敏后的完整正文存储为不可变 Markdown 分块，与读取游标、清单绑定和索引队列在同一 SQLite 事务中原子提交。

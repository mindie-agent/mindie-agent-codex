# 显式历史会话导入

中文 | [English](history-import.md) · [返回文档索引](README.md)

本文说明如何通过 `history-import` 入口显式导入用户选定的历史 Codex JSONL 会话文件。完整英文细则见 [`history-import.md`](history-import.md)。

## 1. 显式选择与权限核验

- 日常任务使用、`Stop` 事件、更新检查与知识同步绝不自动扫描或发现历史会话文件。
- 导入前先校验当前请求会话与源会话均处于用户已保存的贡献授权与批准项目目录范围内，不产生二次弹窗，也不激活历史会话。

```sh
python3 /absolute/active/plugin/scripts/bridge.py history-import   --source /absolute/selected/rollout.jsonl
```

## 2. 管线复用

- 显式历史导入完整复用实时 `Stop` 采集的分页读取、确定性公开消息筛选、Gitleaks `8.30.1` 脱敏、不可变分块落盘与原生小模型导航索引管线。

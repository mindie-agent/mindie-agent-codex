# mindie-agent-codex 0.1.4

**中文** · [English](mindie-agent-codex-0.1.4.en.md)

修复 Codex Desktop `0.164.0-alpha.11.1` 的 Hook 信任继承：原生配置将命令哈希和插件内容哈希保存为 `trusted_hash`、`trusted_content_hash` 两个字段，而 `hooks/list` 返回复合标识。更新器现在按这两个字段还原已批准标识后执行原有 compare-and-swap，兼容旧版单字段格式；用户停用 Hook 或修改任一已批准哈希时仍保留其选择。

在 Windows 企业网络的真实原生 API 上，已依据升级前保存的批准记录验证五个 Hook 的信任继承。54 项 Hook、更新器与候选校验测试通过；本版本的完整跨平台合同与发布基准由 CI 和正式 Tag 流水线交付。

本版本继续包含 `0.1.3` 的企业 Git 配置继承修复，并锁定 `knowledge 0.13.4 / remote-dev 0.10.6 / diagnostics 0.5.2`。

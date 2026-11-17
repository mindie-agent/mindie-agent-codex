# mindie-agent-codex 0.1.36：运行时 Skill 与安装引导解耦及显式手动触发双重契约加固

[English](mindie-agent-codex-0.1.36.en.md)

- **安装与授权文档迁出运行时 Skill 目录**：将首次安装与授权说明文档（`workspace-setup.md`、`github-authentication.md`、`user-notice.md`）由 `plugins/mindie-agent/skills/mindie-agent/references/` 归位至仓库级 `docs/install/`，同步更新中英 `README` 与文档索引引用。
- **纯净运行时 `SKILL.md` 边界**：从 `plugins/mindie-agent/skills/mindie-agent/SKILL.md` 彻底移除首次安装引导、`vllm-ascend-workspace` 克隆初始化、`gh auth login` 登录引导、`setup.py sharing` 首次弹窗与 GitHub Star 提示，仅保留会话激活后的知识检索、分层参考与长周期运行契约。
- **双重显式手动触发声明与冻结 Hook 保持不变**：在保留 `agents/openai.yaml`（`allow_implicit_invocation: false`）与字节级冻结 `hooks/hooks.json`（SHA-256 不变）的同时，在 `SKILL.md` frontmatter 同步声明 `disable-model-invocation: true`，确保 `/mindie-agent` 与 `$mindie-agent` 手动门禁一致。

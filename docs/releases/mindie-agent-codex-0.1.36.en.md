# mindie-agent-codex 0.1.36: Runtime Skill vs. Install-Guide Separation and Explicit Manual-Trigger Hardening

[中文](mindie-agent-codex-0.1.36.md)

- **Relocate Setup & Consent Guides to `docs/install/`**: Moves installation-only documents (`workspace-setup.md`, `github-authentication.md`, and `user-notice.md`) out of `plugins/mindie-agent/skills/mindie-agent/references/` into repository-level `docs/install/`, updating all bilingual `README` and documentation index links.
- **Pure Runtime `SKILL.md` Boundary**: Removes first-use onboarding prompts, `vllm-ascend-workspace` initialization guides, `gh auth login` instructions, `setup.py sharing` onboarding flows, and GitHub Star prompts from `plugins/mindie-agent/skills/mindie-agent/SKILL.md`, keeping shipped skill content strictly scoped to active runtime operations.
- **Explicit Manual-Invocation Guardrails**: Adds `disable-model-invocation: true` to `SKILL.md` frontmatter alongside `agents/openai.yaml` (`allow_implicit_invocation: false`) while preserving the byte-frozen `hooks/hooks.json` SHA-256 digest.

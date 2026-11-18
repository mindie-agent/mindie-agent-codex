# 贡献指南 (`mindie-agent-codex`)

English version: [`CONTRIBUTING.en.md`](CONTRIBUTING.en.md)

感谢参与 `mindie-agent-codex`（OpenAI Codex 原生插件适配层）开发。提交变更前请遵循以下工程契约：

## 1. 仓库职责与核心红线

1. **宿主信任与线协议契约（`HOST-NATIVE` / `HOST-WIRE`）**：
   - `plugins/mindie-agent/hooks/hooks.json` 的字节内容与 SHA-256（`ea80d0815485ba5f762ff5275f1e40817130694870405237dbd65d03f28f9981`）已冻结，用于保持跨版本 Codex 钩子信任连续性。
   - `.mcp.json` 仅通过薄路径定位转发至 `hook_launcher.py` / `mcp_entry.py`，严禁内联复杂 JSON 状态机。
   - 共享进程监管、文件锁与 RSI 循环统一委托给 `mindie-harness`，本仓仅保留 Codex 专有的会话身份解析、转录适配（`codex_transcript.py`）与宿主配置绑定。
2. **静默默认启用与准入边界（`HOST-ADMISSION`）**：
   - `mindie-remote-dev` 与 `mindie-tools`（含 `knowledge_*`）在普通会话、Desktop Work 模式、自动化任务与子代理中必须零前置门禁开箱可用；只读工具（`remote_read`、`remote_grep`、`knowledge_query`、`knowledge_explain` 等）不占用写并发槽位，也不得在热路径扫描 `.owner.lock`。
   - `SessionStart` 与 `UserPromptSubmit` 保持被动零 SQLite 直通；`Stop` 优先检查社区贡献授权与 `project_roots`，未开启或越界时 `< 100ms` 零 SQLite 退出。
3. **升级兼容与状态诚实（`HOST-UPGRADE`）**：
   - 保持 `codex.json`、`prepare.lock` 与 `releases/<version>/runtime.json` 指针兼容，支持旧版本平滑升级与回滚。
   - 严格区分 `completed`、`failed` 与 `unknown`；捕获 `OSError` 前必须显式 `except TimeoutError: raise`。
4. **行尾与模块规模**：
   - 所有文本文件统一使用 Unix `LF`（`\n`）行尾（通过 `python3 .github/scripts/check_lf_eol.py` 校验）。
   - 核心 Python 模块保持 `< 800` 行，并在前 30 行内声明 `[Role]` / `[Invariants]` / `[Timeouts & Locks]` / `[Side-Effect Policy]`。

## 2. 本地验证与提交前检查

1. 在隔离环境变量下运行完整测试套件：`pytest -q tests/`。
2. 校验发布元数据与接口注册表：`python3 plugins/mindie-agent/scripts/setup.py release-metadata --source plugins/mindie-agent --tests tests --revision "$(git rev-parse HEAD)"`。
3. 校验 LF 行尾与 PR 自查清单（`.github/pull_request_template.md` 中的 `HOST-NATIVE`、`HOST-WIRE`、`HOST-ADMISSION`、`HOST-UPGRADE`）：
   ```bash
   python3 .github/scripts/check_lf_eol.py
   python3 .github/scripts/check_pr_checklist.py --profile host-plugin --body-file <pr_body.md>
   ```

# 贡献指南 (`mindie-agent-codex`)

English version: [`CONTRIBUTING.en.md`](CONTRIBUTING.en.md)

感谢参与 `mindie-agent-codex`（OpenAI Codex 原生插件适配层）开发。提交变更前请遵循以下工程契约：

## 1. 仓库职责与核心红线

1. **宿主信任与线协议契约（`HOST-NATIVE` / `HOST-WIRE`）**：
   - `plugins/mindie-agent/hooks/hooks.json` 的字节内容与 SHA-256（`ea80d0815485ba5f762ff5275f1e40817130694870405237dbd65d03f28f9981`）已冻结，用于保持跨版本 Codex 钩子信任连续性。
   - `.mcp.json` 仅通过薄路径定位转发至 `hook_launcher.py` / `mcp_entry.py`（同时覆盖 Windows `%APPDATA%/mindie-agent/codex.json` 与 POSIX `XDG_CONFIG_HOME` 回退定位），严禁内联复杂 JSON 状态机。
   - 共享进程监管、文件锁（`.codex.json.update.lock`）、9 域内容寻址引擎配置（`ensure_knowledge_binding`）、`Stop` 采集交接（`run_stop_capture`）与 RSI 循环（`rsi_loop` / `experiment_ledger`）统一委托给 `mindie-harness`（`>= 0.1.5`），本仓仅保留 Codex 专有的会话/轮次身份解析、按 `(thread_id, turn_id)` 精确匹配的 `mindie_interrupt` 协调、转录适配（`codex_transcript.py`）与宿主配置绑定。
2. **静默默认启用与准入边界（`HOST-ADMISSION`）**：
   - `mindie-remote-dev`（10 项 Codex 工具）与 `mindie-tools`（含 `npu_top`、`mindie_doctor`、`rsi_loop`、`experiment_ledger` 及 5 项 `knowledge_*`）在普通会话、Desktop Work 模式、自动化任务与子代理中必须零前置门禁开箱可用；只读工具（`remote_probe`、`remote_job_status`、`knowledge_query`、`knowledge_explain` 等）不占用写并发槽位，也不得在热路径扫描 `.owner.lock`。
   - 冷/热 `SessionStart`（`< 30ms` 仅原子写入 `pending-update-check.json`，绝不同步执行 `uv sync`）与 `UserPromptSubmit` 保持被动零 SQLite 直通；`Stop` 优先检查社区贡献授权与 `project_roots`，未开启或越界时 `< 100ms` 零 SQLite 退出。
3. **升级兼容与状态诚实（`HOST-UPGRADE`）**：
   - 保持 `codex.json`、`.codex.json.update.lock`、`.prepare-codex.lock` 与 `releases/<version>/runtime.json` 指针兼容，运行时代际回收（`Active + Rollback <= 2`）唯一交由 `mindie-harness` 管理。
   - 严格区分 `completed`、`failed` 与 `unknown`；捕获 `OSError` 前必须显式 `except TimeoutError: raise`。
4. **行尾与模块规模**：
   - 所有文本文件统一使用 Unix `LF`（`\n`）行尾（通过 `uv run --no-project python .github/scripts/check_lf_eol.py` 校验）。
   - `plugins/mindie-agent/scripts/*.py` 总行数保持 `<= 1,100` 行，单模块 `< 800` 行，并在前 30 行内声明 `[Role]` / `[Invariants]` / `[Timeouts & Locks]` / `[Side-Effect Policy]`。

## 2. 本地验证与提交前检查

1. 在隔离环境变量下运行完整测试套件：`uv run pytest -q tests/`。
2. 校验发布元数据与接口注册表：`uv run --no-project python plugins/mindie-agent/scripts/setup.py release-metadata --source plugins/mindie-agent --tests tests --revision "$(git rev-parse HEAD)"`。
3. 校验 LF 行尾与 PR 自查清单（`.github/pull_request_template.md` 中的 `HOST-NATIVE`、`HOST-WIRE`、`HOST-ADMISSION`、`HOST-UPGRADE`，CI 同步运行 `verify-host-plugin.yml` `host: codex`）：
   ```bash
   uv run --no-project python .github/scripts/check_lf_eol.py
   uv run --no-project python .github/scripts/check_pr_checklist.py --profile host-plugin --body-file <pr_body.md>
   ```

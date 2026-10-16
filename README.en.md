<p align="center">
  <img src="https://raw.githubusercontent.com/mindie-agent/mindie-agent/main/assets/brand/banner.svg" alt="MindIE Agent Codex Plugin" width="900">
</p>

# MindIE Agent · OpenAI Codex CLI Plugin

[中文](README.md) | English

**Native OpenAI Codex CLI Plugin for Ascend Inference Engineering: Domain Knowledge, Local Experience Loop, and SSH Remote NPU Execution**

`mindie-agent-codex` is the native OpenAI Codex CLI plugin for [MindIE Agent](https://github.com/mindie-agent/mindie-agent) (initial release `v0.0.1`, focused on the `vLLM` / `vLLM-Ascend` domain), built on the [Nine Design Principles](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.en.md).

Work directly inside your own business repository and native Codex tasks: the plugin provides one informational entry Skill, three domain knowledge tools (query, block read, and optional usefulness feedback), and the full `remote-dev` SSH remote execution toolset.

> Read the [Contribution Notice / 用户须知](plugins/mindie-agent/skills/mindie-agent/references/user-notice.md) for the scope of visible-message sharing, deterministic redaction limits, model-provider processing, and public Git history. For an optional multi-task Git worktree workspace scaffold for `vllm` and `vllm-ascend`, see [vllm-ascend-workspace](https://github.com/mindie-agent/vllm-ascend-workspace) and [workspace setup](plugins/mindie-agent/skills/mindie-agent/references/workspace-setup.md).

---

## Key Features

- **Zero Daily Ceremony**: Use domain knowledge and remote NPU tools on demand inside normal Codex sessions without manual per-task activation or closing steps.
- **Two-Layer Knowledge & Experience Loop ([`mindie-knowledge`](https://github.com/mindie-agent/knowledge))**:
  - Synchronizes curated public packages from [`knowledge-vllm-ascend`](https://github.com/mindie-agent/knowledge-vllm-ascend) with local BM25 and block-citation search.
  - When community sharing is explicitly enabled, deterministically projects and redacts public conversation messages (with Gitleaks 8.30.1 and privacy rules), indexes navigation blocks via the native small model, and proposes public GitHub PRs.
- **SSH Remote Ascend Development ([`mindie-remote-dev`](https://github.com/mindie-agent/remote-dev))**: Editor-semantics remote file operations, patching, managed background jobs, live log streaming, and artifact sync over SSH—independent of experience capture consent.
- **Bounded Local Diagnostics & Opt-In Fault Reporting ([`diagnostics`](https://github.com/mindie-agent/diagnostics))**: Structured local fault logs with an independent opt-in switch for automatic GitHub Issue reporting.

---

## Requirements & Supported Matrix (`v0.0.1`)

- **Prerequisites**: `git`, `uv`, and an authenticated `codex` CLI with native plugin support.
  - **Native Windows**: Git for Windows provides the supported SSH executable.
  - **Linux / Windows WSL**: Use Linux-native `codex`, `git`, and `uv` inside WSL with profile, SQLite, and runtime files on the Linux filesystem (do not invoke Windows binaries across `/mnt/c`). Service-backed knowledge operations require a reachable `systemd --user` manager (`>= 254` with unified cgroup v2 hierarchy; verify with `systemctl --user show --property=Version`).
- **Verified Host Versions** (declared in [`harness-compatibility.json`](plugins/mindie-agent/harness-compatibility.json)):
  - macOS: `codex-cli =0.162.0-alpha.17.2`
  - Native Windows / Windows WSL: `codex-cli =0.153.4`

---

## Quick Start (Install & Verify)

Run the following commands in a macOS/WSL shell or native Windows PowerShell using the same Codex profile throughout to install the immutable `v0.0.1` release:

```sh
git --version
uv --version
codex --version
codex plugin marketplace add https://github.com/mindie-agent/mindie-agent-codex.git --ref v0.0.1
codex plugin add mindie-agent@mindie-agent --json
codex plugin list --json
```

1. Confirm `codex plugin list --json` reports `mindie-agent@mindie-agent` version `0.0.1` enabled in your target profile.
2. Start a new native Codex session to trigger `SessionStart` and MCP initialization (`uv sync --locked --no-editable` builds the isolated locked runtime under `PLUGIN_DATA`; the declared initial startup window is up to 10 minutes).
3. Verify native tool discovery exposes both `mindie-knowledge` and `mindie-remote-dev`.

---

## Everyday Usage & Optional Operator Commands

Ordinary tasks require no operator commands. To explicitly inspect status, adjust sharing consent, import selected historical transcripts, or manage fault reporting, invoke `bridge` via the pinned release `entry.py`:

| Action | Command Example | Description |
|---|---|---|
| **Inspect Status** | `python3 <PLUGIN_DATA>/releases/0.0.1/entry.py bridge status` | Read-only check of active binding, knowledge service, capture cursor, and sync state |
| **Configure Sharing Scope** | `python3 <PLUGIN_DATA>/releases/0.0.1/entry.py bridge configure` | Configure or update community contribution consent and project scope |
| **Disable Sharing** | `python3 <PLUGIN_DATA>/releases/0.0.1/entry.py bridge sharing-disable` | Immediately stop capture and outbound contribution for the configured scope |
| **Refresh Public Feed** | `python3 <PLUGIN_DATA>/releases/0.0.1/entry.py bridge sync` | Trigger an explicit model-free public knowledge feed refresh |
| **Import Historical Session** | `python3 <PLUGIN_DATA>/releases/0.0.1/entry.py bridge history-import --source /path/to/rollout.jsonl` | Import an explicitly selected native JSONL session (see [Historical Contribution](docs/history-import.md)) |
| **Manage Fault Reporting** | `python3 <PLUGIN_DATA>/releases/0.0.1/entry.py bridge reporting-status` | Query or toggle (`reporting-enable` / `reporting-disable`) opt-in tool-fault reporting |

### Stop or Uninstall

Close active tasks using the plugin and retire its knowledge service before removing the plugin via native Codex controls:

```sh
codex plugin remove mindie-agent@mindie-agent --json
```

---

## Silent Updates & Known Limitations

- **Silent Compatible Updates**: More than 24 hours after the last successful check, the next natural MCP call starts an asynchronous background check for the highest compatible ordinary Release, prepares the locked runtime outside the operation lock, and switches atomically when idle while preserving saved consent and reporting settings.
- **Known Host Limitation (Child-Agent Cancellation)**: In `0.0.1`, interrupting a child agent turn in Codex may leave its pending local helper/SSH operation or an already-started remote command running. See [Harness Interface Evidence — Known Limitation](docs/harness-interfaces.md#known-limitation-child-agent-cancellation) ([中文说明](docs/harness-interfaces.zh.md#known-limitation-child-agent-cancellation)). Durable remote background jobs remain governed by their own `remote-dev` lifecycle.

---

## Documentation Index

See the complete documentation index at [docs/README.en.md](docs/README.en.md) ([中文](docs/README.md)):

| English Document | 中文 | Description |
|---|---|---|
| [Harness Interface Evidence](docs/harness-interfaces.md) | [Codex Harness 接口兼容性证据](docs/harness-interfaces.zh.md) | Registered interfaces in `harness-compatibility.json`, native management, main-thread interrupt, and child-agent limitation |
| [Public Transcript Capture](docs/public-transcript.md) | [公开对话采集与脱敏](docs/public-transcript.zh.md) | Native `response_item` projection, Gitleaks 8.30.1 incremental scanning, and immutable block persistence |
| [Explicit Historical Contribution](docs/history-import.md) | [显式历史会话导入](docs/history-import.zh.md) | Consent verification and incremental redaction pipeline for importing selected historical JSONL transcripts |
| [Installed Service Identity](docs/service-continuity.md) | [已安装服务身份与连续性](docs/service-continuity.zh.md) | Isolated locked runtime outside disposable caches, single canonical binding, and idle cutover |
| [Failure Diagnostics](docs/failure-diagnostics.md) | [故障诊断与取消边界](docs/failure-diagnostics.zh.md) | Read-only status diagnostics, local process-group cancellation cleanup, and durable remote job separation |
| [Framework Stability](docs/framework-stability.md) | [框架稳定性与持久化配置](docs/framework-stability.zh.md) | Persistent user consent across updates, Windows atomic config I/O, and verification boundaries |
| [Test Boundaries & Contracts](docs/testing-contracts.md) | [测试边界与契约校验](docs/testing-contracts.zh.md) | `tests/preflight.py` checks, pinned git revision verification, and contract test matrix |

---

## Running Contract & Unit Tests

```sh
export MINDIE_CORE_REPO=/path/to/knowledge-checkout
export MINDIE_KIMI_REPO=/path/to/kimi-adapter-checkout
uv run --project plugins/mindie-agent --no-sync python -m unittest discover -s tests -p 'test_*.py'
```

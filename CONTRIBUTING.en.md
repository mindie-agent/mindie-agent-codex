# Contributing to `mindie-agent-codex`

中文版：[`CONTRIBUTING.md`](CONTRIBUTING.md)

Thank you for contributing to `mindie-agent-codex`, the native OpenAI Codex plugin adapter for MindIE Agent.

## 1. Architecture & Guardrails

1. **Host Trust & Wire Contracts (`HOST-NATIVE` / `HOST-WIRE`)**:
   - `plugins/mindie-agent/hooks/hooks.json` is byte-frozen (`sha256 == ea80d0815485ba5f762ff5275f1e40817130694870405237dbd65d03f28f9981`) so Codex hook trust survives plugin cache updates.
   - `.mcp.json` uses a thin file locator (resolving both Windows `%APPDATA%/mindie-agent/codex.json` and POSIX `XDG_CONFIG_HOME` fallbacks) without inline JSON state-machine blobs.
   - Delegate shared process supervision, file locking (`.codex.json.update.lock`), 9-domain content-addressed engine provisioning (`ensure_knowledge_binding`), `Stop` capture (`run_stop_capture`), and RSI tools (`rsi_loop`, `experiment_ledger`) to `mindie-harness` (`>= 0.1.5`); keep Codex-specific logic focused on native session/turn identity, turn-scoped `(thread_id, turn_id)` `mindie_interrupt` correlation, `codex_transcript.py`, and profile binding.
2. **Silent Default-On Tools & Admission Boundaries (`HOST-ADMISSION`)**:
   - `mindie-remote-dev` (10 Codex tools) and `mindie-tools` (including `npu_top`, `mindie_doctor`, `rsi_loop`, `experiment_ledger`, and the 5 `knowledge_*` tools) must work out of the box in every session, Desktop Work mode, Automation, and Subagent without prompt-prefix gates or `_meta` turn headers; read-only tools (`remote_probe`, `remote_job_status`, `knowledge_query`, `knowledge_explain`) bypass mutation concurrency caps and hot-path `.owner.lock` scans.
   - Cold and warm `SessionStart` (`< 30ms` marker-only `pending-update-check.json`, zero synchronous `uv sync`) and `UserPromptSubmit` remain passive with zero cold-start SQLite writes; `Stop` checks community sharing consent and `project_roots` first (`< 100ms` exit without SQLite when unconfigured, disabled, or out of scope).
3. **Upgrade Continuity & State Honesty (`HOST-UPGRADE`)**:
   - Maintain backward compatibility with `codex.json`, `.codex.json.update.lock`, `.prepare-codex.lock`, and `releases/<version>/runtime.json` across upgrades and rollbacks while leaving runtime generation GC (`Active + Rollback <= 2`) solely to `mindie-harness`.
   - Distinguish `completed`, `failed`, and `unknown` external effects, and precede every `except OSError:` handler with `except TimeoutError: raise`.
4. **Line Endings & Module Size**:
   - Use Unix `LF` (`\n`) line endings across all text files (`uv run --no-project python .github/scripts/check_lf_eol.py`).
   - Keep `plugins/mindie-agent/scripts/*.py` within `<= 1,100` total lines and each module `< 800` lines with a top-30-line `[Role]` / `[Invariants]` / `[Timeouts & Locks]` / `[Side-Effect Policy]` docstring.

## 2. Pre-PR Verification

```bash
uv run --no-project python .github/scripts/check_lf_eol.py
uv run pytest -q tests/
uv run --no-project python plugins/mindie-agent/scripts/setup.py release-metadata --source plugins/mindie-agent --tests tests --revision "$(git rev-parse HEAD)"
uv run --no-project python .github/scripts/check_pr_checklist.py --profile host-plugin --body-file <pr_body.md>
```

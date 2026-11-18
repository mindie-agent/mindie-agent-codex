# Contributing to `mindie-agent-codex`

中文版：[`CONTRIBUTING.md`](CONTRIBUTING.md)

Thank you for contributing to `mindie-agent-codex`, the native OpenAI Codex plugin adapter for MindIE Agent.

## 1. Architecture & Guardrails

1. **Host Trust & Wire Contracts (`HOST-NATIVE` / `HOST-WIRE`)**:
   - `plugins/mindie-agent/hooks/hooks.json` is byte-frozen (`sha256 == ea80d0815485ba5f762ff5275f1e40817130694870405237dbd65d03f28f9981`) so Codex hook trust survives plugin cache updates.
   - `.mcp.json` uses a thin file locator without inline JSON state-machine blobs.
   - Delegate shared process supervision, file locking, and RSI helpers (`rsi_loop.py`, `rsi_explore.py`, `experiment_ledger.py`) to `mindie-harness`; keep Codex-specific logic focused on native session identity, `codex_transcript.py`, and profile binding.
2. **Silent Default-On Tools & Admission Boundaries (`HOST-ADMISSION`)**:
   - `mindie-remote-dev` and `mindie-tools` (including `knowledge_*`) must work out of the box in every session, Desktop Work mode, Automation, and Subagent without prompt-prefix gates or `_meta` turn headers; read-only tools (`remote_read`, `remote_grep`, `knowledge_query`, `knowledge_explain`) bypass mutation concurrency caps and hot-path `.owner.lock` scans.
   - `SessionStart` and `UserPromptSubmit` remain passive with zero cold-start SQLite writes; `Stop` checks community sharing consent and `project_roots` first (`< 100ms` exit without SQLite when unconfigured, disabled, or out of scope).
3. **Upgrade Continuity & State Honesty (`HOST-UPGRADE`)**:
   - Maintain backward compatibility with `codex.json`, `prepare.lock`, and `releases/<version>/runtime.json` across upgrades and rollbacks.
   - Distinguish `completed`, `failed`, and `unknown` external effects, and precede every `except OSError:` handler with `except TimeoutError: raise`.
4. **Line Endings & Module Size**:
   - Use Unix `LF` (`\n`) line endings across all text files (`python3 .github/scripts/check_lf_eol.py`).
   - Keep core Python modules `< 800` lines with a top-30-line `[Role]` / `[Invariants]` / `[Timeouts & Locks]` / `[Side-Effect Policy]` docstring.

## 2. Pre-PR Verification

```bash
python3 .github/scripts/check_lf_eol.py
pytest -q tests/
python3 plugins/mindie-agent/scripts/release_metadata.py --source plugins/mindie-agent --tests tests --revision "$(git rev-parse HEAD)"
python3 .github/scripts/check_pr_checklist.py --profile host-plugin --body-file <pr_body.md>
```

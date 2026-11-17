# mindie-agent-codex 0.1.35: `mindie-harness` Shared Substrate Integration, Concurrent Read-Only MCP, and `0.1.13` Component Baseline

[中文](mindie-agent-codex-0.1.35.md)

- **Shared `mindie-harness v0.1.0` Substrate Adoption**: Bundles reproducible `assets/mindie-harness-seed.pyz` + `seed-lock.json` (`mindie-harness-seed/1`) and delegates shared file locking, `ExperimentLedger` / RSI loop execution (`experiment_ledger.py`, `rsi_loop.py`, `rsi_explore.py`), and `HostAdapter` contracts to `mindie_harness`.
- **Concurrent Read-Only MCP & Anti-Pattern Reversal (`AD-07/09/10/12`)**: Unblocks read-only/status MCP calls from foreground write locks, supports local symlinked plugin development roots, removes local one-way self-bricking states, and preserves the frozen `hooks/hooks.json` SHA-256 byte digest.
- **`0.1.13` Formal Component Baseline**: Locks `mindie-harness v0.1.0`, `mindie-knowledge v0.13.23`, `remote-dev v0.11.13`, `mindie-diagnostics v0.5.16`, `mindie-debugger v0.1.4`, `mindie-profiler v0.5.16`, `mindie-sim v0.9.18`, and `mindie-coordinator v0.7.13` in `plugins/mindie-agent/pyproject.toml` and `uv.lock`, aligned with `--profile host-plugin` governance.

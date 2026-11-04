# mindie-agent-codex 0.1.19

`mindie-agent-codex 0.1.19` preserves Codex native hook trust across plugin updates without requiring manual re-approval (`native_hooks.py` / `harness_update.py` / `release_migration.py`), enforces Windows zero-console-popup execution across all bootstrap and updater subprocesses (`bootstrap.py`), stops classifying transient catalog misses as `update_internal_failure` (`#86`), and aligns all runtime component pins (`mindie-knowledge 0.13.12`, `remote-dev 0.11.5`, `mindie-diagnostics 0.5.6`, `mindie-coordinator 0.7.4`).

| Component | Version | Source commit |
| --- | --- | --- |
| knowledge | 0.13.12 | `1840bd4e238e718676768489058917818d18cd3c` |
| remote-dev | 0.11.5 | `131b24533479e9cefc572144adccdcb345f70846` |
| diagnostics | 0.5.6 | `9b331956b00737dd6d8c956da3416316f2de1ddf` |
| debugger | 0.1.0 | `e9406b2b178335190d5cb59dff4d6985894497ba` |
| profiler | 0.5.6 | `79d97e75bff7351b0472b93c03b072c1cf47f5e7` |
| sim | 0.9.3 | `dbadff04b60937a0722b0a227cdad01beed083cd` |
| coordinator | 0.7.4 | `14d7cec7247bdcbea9b9878db4770e3a05a6ea41` |
| npu-top | 0.3.1 | `8f0cb3ec66393812e90e118cd73704070c6027a2` |
| mindie-bench | 0.4.6 | `c7d0c8a4969b3816925c0405931ae17e28ce7a34` |

## What's New (Human-Readable Summary)

- **Zero Re-Approval Hook Trust Preservation Across Updates**: Reconciles all discovered Codex CLI and ChatGPT Desktop binaries on the machine (`native_context.py`), syncs `hooks/hooks.json` in place across every active Codex plugin cache when the command string is unchanged, and falls back to `app-server` CAS reconciliation only when needed so users are never prompted to re-allow hooks on plugin updates.
- **Windows Zero Console Popup Hardening**: Replaces `os.execve` on Windows in `bootstrap.py` with `subprocess.run(..., creationflags=CREATE_NO_WINDOW)` and sanitized stdio handles, eliminating transient console window flashes from GUI hosts.
- **Catalog Miss Classification Fix (`#86`)**: Stops reporting transient `component_update` catalog misses as `update_internal_failure`.

# mindie-agent-codex 0.1.22

[简体中文](mindie-agent-codex-0.1.22.md)

This patch updates the Codex MCP catalog after independent component upgrades. `remote_probe` now advertises and accepts `distributions`, `python_executable`, `python_candidates`, and `required_distributions`. Previously, an updated component runtime supported these fields while Codex's fixed catalog rejected them at the host boundary.

- Regenerate the complete Codex catalog from pinned remote-dev 0.11.7, including shared guidance for selected source directories, business interpreters, and continuation with task scripts.
- Pin formal remote-dev 0.11.7 and coordinator 0.7.6 in the bootstrap runtime. Independent component updates continue to retain installed version floors.
- Forward all four arguments through native MCP validation to the shared probe. Explicit interpreter failures stay failures, multiple eligible environments report ambiguity, and virtualenv identity is preserved.
- Load the new catalog at the host's existing plugin refresh boundary. Component activation and host tool-definition loading are verified separately while retaining active sessions.

Validation covers catalog generation, actual argument validation and runtime forwarding, upgrades from old installations, cross-platform CI, and exact-tag release benchmarks. Native rebuilds, dynamic-library loading, and A2/A3 NPU canary acceptance are outside this patch's completed validation.

## Formal dependency combination

| Component | Version | Source commit |
| --- | --- | --- |
| knowledge | 0.13.13 | `3c478a19b5542b38520c7f98b87a07f37e305021` |
| remote-dev | 0.11.7 | `6a64da8be9ed7c0bb38c2ba57f539da9a43ddf34` |
| diagnostics | 0.5.7 | `f338788cecc56f7aff1dccab1d53426c9e28fe7f` |
| debugger | 0.1.0 | `e9406b2b178335190d5cb59dff4d6985894497ba` |
| profiler | 0.5.6 | `79d97e75bff7351b0472b93c03b072c1cf47f5e7` |
| sim | 0.9.4 | `6f1d5a871a7b1ef20ff7a8cab33aaf4637fb3f66` |
| coordinator | 0.7.6 | `8690fdd065c73adab8dae2542d6a374f2ecab59e` |

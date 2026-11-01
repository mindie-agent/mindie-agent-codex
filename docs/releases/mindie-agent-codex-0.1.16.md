# mindie-agent-codex 0.1.16

This maintenance release synchronizes the native `sim` and `profiler` MCP tool
descriptions with `mindie-sim 0.9.0` (`f5608b4943f515c401d2f9b12fffb579a1e2571b`)
and `mindie-profiler 0.5.5` (`7cdd56b6cc9e0e7f603368393497487dd2dbfe39`),
streamlines plugin skill and tool prompt wording, and includes
enterprise-network transport hardening and Windows/LF install fixes.

| Component | Version | Source commit |
| --- | --- | --- |
| knowledge | 0.13.8 | `9a4325858e6fadb10715275826a7795843cbbdf4` |
| remote-dev | 0.11.2 | `85be999811771e03ecc7d78e5e560fac1636e7ef` |
| diagnostics | 0.5.4 | `5ae643e19223f2181ecdbaa3edaf464137c4805e` |

## Key updates

- Expose `mindie-sim 0.9.0` and `mindie-profiler 0.5.5` capabilities in the
  `mindie-tools` MCP catalog: Ascend A2/A3/A5 hardware bounds, `compose`
  (`--model`, `--hardware`, `--concurrency`, `--async-scheduling`),
  `predict`, Roofline `efficiency`, `sla` concurrency frontiers, P/D
  disaggregation `sweep` (`--mode pd`), `skew`, and `profiler` (`camodel`,
  `kernels`).
- Streamline `SKILL.md`, reference guides, and MCP tool descriptions to remove
  repetitive defensive boilerplate while preserving all operational contracts.
- Harden locked runtime preparation for enterprise TLS/proxy environments
  (`UV_SYSTEM_CERTS` / `UV_NATIVE_TLS`, `GCM_INTERACTIVE=never`) and Windows
  Git transport (`core.longpaths=true`, `core.autocrlf=false`, and GitHub
  credential provider bindings).

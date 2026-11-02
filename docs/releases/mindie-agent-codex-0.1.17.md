# mindie-agent-codex 0.1.17

This release replaces the single-point upper bound (`0.166.0` / `0.166.0-alpha.5.1`)
in `plugins/mindie-agent/harness-compatibility.json` with a forward-compatible
`0.x` SemVer interval (`>=0.162.0-alpha.17.2 <=0.999.999`, `include_prerelease: true`,
alongside `0.153.4`) so ChatGPT Desktop and `codex-cli` minor, patch, and alpha
upgrades continue to pass setup and background updates without falling into
`no_compatible_release`, and clarifies the remote container CANN `plog` triage
workflow in the `debugger` MCP tool description.

| Component | Version | Source commit |
| --- | --- | --- |
| knowledge | 0.13.8 | `9a4325858e6fadb10715275826a7795843cbbdf4` |
| remote-dev | 0.11.2 | `85be999811771e03ecc7d78e5e560fac1636e7ef` |
| diagnostics | 0.5.4 | `5ae643e19223f2181ecdbaa3edaf464137c4805e` |

## Key updates

- Widen `harness-compatibility.json` (`supported` and all 12 registered host
  interfaces) to cover `0.153.4` and `>=0.162.0-alpha.17.2 <=0.999.999`
  (`include_prerelease: true`), preventing host CLI or ChatGPT Desktop bundled
  `codex-cli` upgrades from blocking installation or auto-updates.
- Clarify the remote container CANN `plog` triage workflow in the `debugger`
  MCP tool description (`remote-guide` + `remote-dev` NPU container log scan).

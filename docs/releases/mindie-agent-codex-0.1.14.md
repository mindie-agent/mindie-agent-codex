# mindie-agent-codex 0.1.14

This maintenance release aligns the native component bridge, post-verification
convergence, and distillation output-schema handling with `mindie-knowledge`
`0.13.8` and `remote-dev` `0.11.2`, and extends native Codex host compatibility
to cover `0.166.0-alpha.5.1`.

| Component | Version | Source commit |
| --- | --- | --- |
| knowledge | 0.13.8 | `9a4325858e6fadb10715275826a7795843cbbdf4` |
| remote-dev | 0.11.2 | `85be999811771e03ecc7d78e5e560fac1636e7ef` |
| diagnostics | 0.5.4 | `5ae643e19223f2181ecdbaa3edaf464137c4805e` |

## Native host compatibility

The compatibility declaration adds the exact installed Desktop CLI version
`0.166.0-alpha.5.1`. Earlier declarations only covered up to
`0.165.0-alpha.1.2`, causing the normal background selector to report
`no_compatible_release` after Desktop CLI auto-upgrade.

Release preparation validates the actual executable in an isolated native
profile, without model requests: plugin installation, five hooks, trust
compare-and-swap, explicit disable preservation, locked runtime setup,
SessionStart and MCP discovery. Existing contract tests cover the remaining
registered protocol boundaries.

## Key fixes and improvements

- Align component bridge and post-verification convergence, and split `mcp_gate`
  into `<800` line modules (`mcp_gate.py` + `mcp_identity.py`).
- Omit strict `--output-schema` when C6 distillation schemas contain `anyOf`
  and strip markdown code fences from structured output.
- Join reader threads after terminating owned process trees in `process_guard`.
- Update `product-contract.json` `verified_commit` to
  `1c7bcad61f1156389dcf714468acd74c34754268` (`knowledge-vllm-ascend`) paired
  with `mindie-knowledge 0.13.8` (`9a4325858e6fadb10715275826a7795843cbbdf4`).

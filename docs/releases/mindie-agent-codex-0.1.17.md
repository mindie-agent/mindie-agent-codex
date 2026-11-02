# mindie-agent-codex 0.1.17

`mindie-agent-codex 0.1.17` brings forward-compatible host version support so upgrading ChatGPT Desktop or `codex-cli` within `0.x` no longer blocks plugin setup or background updates, and upgrades the bundled runtime components to the latest Ascend AI Infra suite (`knowledge 0.13.10`, `remote-dev 0.11.4`, `diagnostics 0.5.5`, plus full `debugger 0.1.0`, `profiler 0.5.6`, `sim 0.9.2`, `coordinator 0.7.3`, and `npu-top 0.3.1` support).

| Component | Version | Source commit |
| --- | --- | --- |
| knowledge | 0.13.10 | `7936a4fda67f6808d8fa73824363a4d7345766e5` |
| remote-dev | 0.11.4 | `7c67876d716feb3949e4dbf33eb5e06980ff0d36` |
| diagnostics | 0.5.5 | `f2f2f946c172afc99342a84c0c1ed244f6d44e01` |
| debugger | 0.1.0 | `e9406b2b178335190d5cb59dff4d6985894497ba` |
| profiler | 0.5.6 | `79d97e75bff7351b0472b93c03b072c1cf47f5e7` |
| sim | 0.9.2 | `ced2dba5741af2d98f3a75b2a9cad77b9b94c40d` |
| coordinator | 0.7.3 | `79a8b28eef72d57f965a01c6660849f024fbb1c0` |
| npu-top | 0.3.1 | `8f0cb3ec66393812e90e118cd73704070c6027a2` |

## What's New (Human-Readable Summary)

- **Smooth Codex CLI & ChatGPT Desktop Upgrades**: Replaces rigid single-version locks (`0.166.0` / `0.166.0-alpha.5.1`) in `plugins/mindie-agent/harness-compatibility.json` with a forward-compatible `0.x` SemVer range (`>=0.162.0-alpha.17.2 <=0.999.999`, `include_prerelease: true`, alongside `0.153.4`) so minor, patch, and alpha host upgrades keep working out of the box.
- **Latest Shared Component & Corpus Baseline**:
  - `mindie-knowledge 0.13.10` (`7936a4fda67f6808d8fa73824363a4d7345766e5`) and `knowledge-vllm-ascend` (`a0f890b70e73919ffe5d917febcb2595793422ef`, 297 verified units) with flexible component runtime support for `mindie-debugger` and `npu-top`.
  - `remote-dev 0.11.4` (`7c67876d716feb3949e4dbf33eb5e06980ff0d36`), `mindie-diagnostics 0.5.5` (`f2f2f946c172afc99342a84c0c1ed244f6d44e01`), `mindie-profiler 0.5.6` (`79d97e75bff7351b0472b93c03b072c1cf47f5e7`), `mindie-sim 0.9.2` (`ced2dba5741af2d98f3a75b2a9cad77b9b94c40d` with Ascend 950PR / A5 Roofline & superpod topology calibration), and `mindie-coordinator 0.7.3` (`79a8b28eef72d57f965a01c6660849f024fbb1c0`).
- **Clearer Remote CANN Log Triage**: Improves the `debugger` MCP tool instructions for inspecting `plog` and ACLGraph faults inside remote NPU containers via `remote-guide` + `remote-dev`.

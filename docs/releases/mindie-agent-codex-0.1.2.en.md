# mindie-agent-codex 0.1.2 Release Notes

[中文](https://github.com/mindie-agent/mindie-agent-codex/blob/v0.1.2/docs/releases/mindie-agent-codex-0.1.2.md) | **English**

`mindie-agent-codex 0.1.2` is a maintenance release for OpenAI Codex CLI (paired with `mindie-agent 0.1.1`, while preserving the existing immutable `0.1.1` plugin tag). It introduces independent formal GitHub Release auto-updates for shared runtime components and tightens cold activation and per-session manual `/mindie-agent` knowledge boundaries.

## Highlights

- **Independent Shared Component Auto-Update & Non-Downgrade Floor**:
  - When no newer plugin Release is selected, background update checks discover formal GitHub Releases directly from official component repositories, selecting the non-downgrading maximum across the currently installed version, the plugin baseline, and the latest formal component Release, and building a side-by-side isolated `uv` runtime with a verified `uv.lock`.
  - When upgrading to a newer plugin Release, runtime setup inherits the installed component version floor so a plugin upgrade never downgrades a component that was already updated independently.
- **Model-Free Cold Candidate Validation, Immutable Native Payload & Atomic Idle Cutover**:
  - Removed preparation-time probing that started a knowledge worker; candidate environments are validated via isolated package inventory checks and a model-free runtime API probe, followed by idle service retirement and atomic binding publication (`codex.json`). The knowledge worker starts only on a subsequent authorized manual knowledge call.
  - All source Python launch paths pass `-B` explicitly (preventing `.pyc` bytecode writes), and baseline `uv sync` runs against a short-lived project copy under `PLUGIN_DATA/releases/<version>/build` cleaned up inside `prepare.lock` so setuptools `build/` and `*.egg-info` artifacts never mutate the installed native plugin cache or invalidate Codex hook trust hashes.
- **Exploration Payload Pass-Through & Explicit Session Gate**:
  - Exploration outputs from `mindie-knowledge` are passed through untouched without host-side rewriting. Software updates never grant session knowledge authority or alter saved user consent; knowledge features remain active only after manually invoking `/mindie-agent` (or `$mindie-agent`) in the current session.

## Runtime Components & Verification Scope

- **Paired Runtime Components**: Targets the formal releases of `mindie-knowledge 0.13.3`, `remote-dev 0.10.6` (`0463a13fce9618ec9a51904869d61acb0cdb8538`), and `mindie-diagnostics 0.5.2` (`801a01b2789aed7f58e971a909205a7d40b61077`). Exact commit SHAs and supported Codex CLI versions (`0.153.4`, `0.162.0-alpha.17.2`, `0.164.0-alpha.11.1`) are recorded in `plugins/mindie-agent/uv.lock`, `product-contract.json`, and `harness-compatibility.json`.
- **Verification & Acceptance Boundary**: Passed the native-payload immutability and component-update suite (`57 passed, 6 subtests`), the component-update and candidate-validation suite (`13 passed, 31 subtests`), the existing update suite (`44 passed, 8 subtests`), and the launcher suite (`24 passed, 2 subtests`). On baseline plugin `0.1.1` with live Codex `0.164`, a fresh passive session was verified to reach `READY` with zero MindIE skill content in the rollout transcript and `0` SQLite knowledge leases for that session; clean native-cache Hook/MCP joint verification for the `0.1.2` candidate and cross-host native new-session acceptance are tracked with the main product release gate.

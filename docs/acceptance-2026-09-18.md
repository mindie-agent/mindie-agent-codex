# Initial closed-loop acceptance — 2026-09-18

Environment: macOS arm64, Codex CLI 0.153.4, Python 3.11.15.

## Scope and observed results

The installed plugin was exercised through two fresh native Codex CLI tasks, its
actual SessionStart/Stop hooks and all three bundled MCP tools. Three independent
service roots represented publisher A, consumer B, and the next consumer C. The
organizer and judge each invoked a fresh ephemeral Codex execution.

The task fixture contained a pure shape helper behind a package initializer that
imports unavailable `torch_npu`. A demonstrated normal import failure and directly
loaded the helper to run three CPU assertions. Its normal final reply was captured
and organized into one experience. B received it through snapshot distribution,
read it, applied the approach to a separate fixture, executed four assertions and
recorded concrete output. The independent judge classified that use as `helpful`.

| Same reference, same query | Before B's feedback | After feedback reached C |
|---|---:|---:|
| Retrieval score | 0.97092699 | 1.37781143 |
| Usefulness multiplier | 1.0 | 1.419068 |
| Independent helpful uses | 0 | 1 |

No maintenance errors were recorded. This proves the control flow and feedback
application on a synthetic CPU fixture. It does not establish NPU correctness,
real-user effectiveness, production-scale retrieval, native Desktop cross-domain
task control, or any other Harness's behavior.

## Automated checks

- Knowledge component: 598 passed, 4 skipped, 12 subtests passed.
- Codex adapter: 3 unit tests passed.
- Plugin and Skill validation passed.
- Knowledge tests cover domain isolation, knowledge applicability, deduplication,
  independent-use constraints, neutral unknown feedback, negative withdrawal,
  snapshot integrity, publication privacy, replica contribution, actual HTTP/MCP,
  failed-judge behavior and bounded offline hooks.

## Runtime integration findings

- Plugin-relative MCP `cwd` plus relative script args works in the tested CLI;
  `${PLUGIN_ROOT}` in compatibility MCP arguments did not expand.
- Explicit `env_vars` forwarding is needed for `MINDIE_AGENT_CONFIG` in the MCP
  process, so that hooks and MCP select the same domain service.
- Test invocations authorized only this plugin's three tools and vetted hooks.
  The test did not persist a global hook-trust or tool-approval bypass.
- The ordinary plugin install still requires native Codex tool authorization and
  hook trust. New tasks pick up the installed plugin.

## Reproduction

Install the repo marketplace and runtime as described in README, then run:

```sh
.venv/bin/python tests/live_acceptance.py --output .local/acceptance/new-run
```

This explicitly runs actual model calls and may consume account usage. The result
JSON, native task tool events and ordinary final replies remain in the selected
local output directory. `.local/` is excluded from Git; no raw conversations or
service credentials are published with this report.

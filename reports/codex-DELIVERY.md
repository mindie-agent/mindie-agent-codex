# Codex adapter delivery (2026-09-20)

Branch `codex/lifecycle-onboarding-20260920`. Component tests only. No push,
no PR, no global install, no model/NPU acceptance (root owns those).

## This pass

- Removed `apply_dependency_overlay` / `settings.dependency_overlay` and its
  test. Production install uses only exact official remote pins.
- Knowledge pin: `1808359f79a1c7fe1eab4853a834178280aa1d70`. remote-dev
  unchanged (`13301ef7f52b53ffca0a6702a8a3c18f2edfcd52`).
- Runtime probe asserts `Engine.stop_if_idle` and `Service._stop_if_idle`.
  Knowledge tools are validated from adapter `mcp_catalog.json`, not a
  retired core `TOOLS` list. Update contract keeps
  `admission_path`/`transcript_adapter`; no legacy `session_activation` shim.
- `check_knowledge` reads config and runs `sync` under the shared operation
  lock so an installer cannot swap generation mid-feed.

## Generation routing (review)

Admission helper, Stop `stop_capture`, knowledge/remote `runtime_call`,
bind, contribution CLI, setup configure, and status/init all select
`python` + `runtime_scripts` + engine config under `update_lock`.
`runtime_call.redispatch` re-execs the recorded script/interpreter.
Updates take the exclusive lock, call core `stop_if_idle`, then write
interpreter, `runtime_scripts`, `transcript_adapter`, and
`admission_path` together. Idle grants do not block.

## Tests

`../acceptance-venv/bin/python -m unittest discover -s tests -v` (no
PYTHONPATH): **105 passed**. Native plugin validator and skill validator
passed.

## Remaining (root)

- Native Codex Luna/max acceptance and publication.
- Production pin install of knowledge `1808359f` on user machines.
- Core PR 45 recovery/cleanup is core-owned; this adapter only wraps
  `contribution-inspect/reconcile/retry/compact --batch`.

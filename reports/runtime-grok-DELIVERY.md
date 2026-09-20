# runtime-grok delivery: mindie_exec / remote-dev contract

Component-only repair. No remote host, NPU, model, or coordinator live
acceptance was run here; Luna/max own that path.

## What changed

Domain remote I/O no longer calls `remote_dev.mcp.tools.call_tool` in-process
and no longer invents a session when adapter config is missing. Every remote
tool goes through `Gate.call` → `runtime_call.py` → the pinned remote-dev
package. MCP jobs and Skill-side `job_status` / `job_tail` / `job_stop` share
the same `REMOTE_DEV_SESSION_ID` (`mindie-{session}`) and
`REMOTE_DEV_STATE_DIR` (`{engine.root}/remote-dev`).

Removed, not shimmed: `ssh_run_bytes`, `ssh_argv`, `ssh_stream`, the
config-missing development bypass, and the Triton `domain-lib/mindie_exec.py`
snapshot (see `domains/triton-ascend/domain-lib/DOMAIN-SPLIT.md`).

## Admission, budget, deadline

| Bound | Value | Where |
| --- | --- | --- |
| Adapter config | must exist | `Gate.call`, `mindie_exec._credentials` |
| Manual lease | `MINDIE_SESSION_ID` + `MINDIE_ACTIVATION` | Gate check + claim |
| Duplicate identity | one claim per request id | `Sessions.claim` kind `mcp` |
| Failure circuit | 3 consecutive plugin failures | `Sessions.finish` |
| Update lock | shared, non-blocking | `update_lock(config)` around dispatch |
| Local RPC budget | 65s remote / 15s knowledge, 1MiB, cancel | `bounded_process.run` |
| Per-call timeout | `job_status` / `job_tail` / `job_stop` `timeout=` | capped at the 65s budget |
| Remote command deadline | `ssh_exec` / `start_job` `timeout` (seconds, required positive for start_job) | remote-dev `timeout_ms` |
| Artifact default | 120s `timeout=` | passed as `timeout_ms`; local Gate still caps the subprocess at 65s |
| Yield | `ssh_exec` ≤ 30s; `start_job` 1s then return `job_id` | remote-dev `yield_time_ms` |

Missing config or lease fails closed. Domain CLI cannot skip the 65s
process bound or the failure circuit.

## Job reference and ownership

- `remote_bash` always carries `job_id` (also after success). Completion is
  `state` / `status` in `{succeeded, failed, timeout, cancelled}` plus
  `exit_code`. Output is `preview.stdout` / `preview.stderr`, never `output`.
- `ssh_exec` returns `subprocess.CompletedProcess[str]`. `check=False` only
  tolerates a completed remote command with non-zero `exit_code`. Auth,
  transport, blocked, truncated preview, and unknown outcome raise
  `RemoteExecutionError` and are not turned into empty strings.
- A still-running command keeps its `job_id` and is observed with
  `remote_job_stdin` / `job_status`. It is not relaunched. `start_job` does
  not catch `TypeError` and retry.
- Truncated completion (`bytes_remaining` with no further preview) is an
  explicit failure; partial preview is not treated as the full result.
- `state` in `{unknown, absent, lost}` or `submission_uncertain` is not
  retried. Owned jobs are cancelled with `job_stop(..., force=)` (schema
  `force`). `ssh_exec` stops the owned job on unexpected local abort.
- `job_status` `outcome=success` means the RPC succeeded. Job liveness is
  `status` / `state` / `job.remote_status`.
- `job_tail` returns the preview dict (`tail` / `stdout` / `stderr`).
- `open_local_forward` is a context manager: admitted, claimed, always
  `close()`/`kill` on exit. It is a local owned SSH process, not a
  `runtime_call` child, so it can outlive one 65s RPC; it cannot be
  detached.

## Caller migration (this worktree)

| Caller | Change |
| --- | --- |
| `vllm-ascend-serving` | already used `.stdout` / `.returncode`; now that contract is real |
| `vllm-ascend-benchmark` | already used `CompletedProcess`; long `ssh_exec` timeouts poll the job |
| `ascend-memory-profiling` | `ssh_write_text` → `artifact_push`; CSV / config / logs / inspector file → `artifact_pull` / `artifact_push`; listings and `npu-smi` stay `ssh_exec` |
| `modelscope` | unchanged (`owned_process` / `pid_alive` / `process_identity`) |
| Triton snapshot | `mindie_exec.py` deleted; skills do not import it |

## Profiling-analysis / collection (root-owned, not edited)

Root must finish migrating these imports. The public contract they need:

```
ssh_exec(endpoint, script, *, check=True, timeout=180, connect_timeout=10,
         container=None, cwd=None) -> CompletedProcess[str]
start_job(endpoint, command, *, name, timeout, container=None, cwd=None,
          env=None) -> job_id
job_status(endpoint, job_id, *, timeout=None, ...) -> result dict
job_tail(endpoint, job_id, *, lines=200, timeout=None, ...) -> preview dict
job_stop(endpoint, job_id, *, force=False, timeout=None, ...) -> result dict
artifact_push(endpoint, local_path, remote_path, *, timeout=120, ...) -> result
artifact_pull(endpoint, remote_path, local_dir, *, timeout=120, ...) -> result
open_local_forward(endpoint, remote_port, **kwargs)  # context manager only
```

Required of those skills:

1. Drop `ssh_argv`, `ssh_run_bytes`, native SSH argv, and stdout tar.
   Real files go through `artifact_push` / `artifact_pull` (result
   `artifacts` list is the hash evidence).
2. Drop `ssh_stream` / `require_transport()["run_stream"]` /
   `mindie_exec.as_endpoint`. `require_transport()` only proves the pin
   imports. Catch `mindie_exec.RemoteExecutionError`.
3. Poll with `job_status` `state`/`status`. Do not treat `job_id` as
   failure, and do not catch `TypeError` then launch again.
4. Export `MINDIE_SESSION_ID` / `MINDIE_ACTIVATION` against
   `MINDIE_AGENT_CONFIG`. Fail closed without them.
5. Collection tunnel: `with open_local_forward(...)` only; do not keep a
   background SSH `-L` process.

Current analysis `_common.py` still imports `ssh_argv` / `ssh_run_bytes` /
`ssh_stream`. Collection `_common.py` still indexes
`require_transport()["RemoteExecutionError"]`. Those will import-fail or
TypeError until root lands the matching edits.

## Tests run

Interpreter: `/Users/maoxx241/code/mindie-release-20260919-154320/acceptance/runtime/bin/python`
(remote-dev@13301ef and coordinator@0191b81 already installed; nothing
was installed or started here).

- `tests.test_mindie_exec` (17) — local SQLite lease, mocked `bounded_process.run` / `call_tool`
- `tests.test_session_gate`, `tests.test_adapter`, `tests.test_entry_bounds`, `tests.test_stop_hook`
- memory-profiling, serving receipt/identity, benchmark, modelscope, Triton skill tests

Not run against a real SSH endpoint, NPU, or model. `tests.test_auto_update`
`test_domain_runtime_repair_is_bounded_and_observable` expects an interpreter
without coordinator; the acceptance runtime has coordinator and reports
`up_to_date`. Unrelated to this contract fix.

## Unsolved limits

- Artifact default timeout is 120s on the remote-dev argument, but the Gate
  subprocess is still killed at 65s. Large pulls/pushes need more than one
  admitted call or a later budget change; this layer does not open a second
  transport to bypass that.
- `ssh_exec` of a long command (bench 1200s, msprof 1800s) launches once and
  polls; each poll is a new 65s Gate call. That preserves capability without
  one unbounded local process.
- `open_local_forward` is admitted and owned but not 65s-bounded, because the
  process *is* the tunnel. Cleanup is context-manager ownership only.
- Preview drain is capped at 1MiB locally. Larger job logs must be pulled as
  artifacts, not via `ssh_exec` stdout.
- No live remote-dev job was observed in this worktree. Hash-evidence
  `artifacts` lists are enforced when the RPC returns; the actual stream was
  not exercised.

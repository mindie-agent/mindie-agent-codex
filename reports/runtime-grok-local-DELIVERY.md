# runtime-grok local ownership

Replaced the `pid_alive` / `process_identity` / `owned_process` stubs.
Remote execution was not changed. No weights, models, or network.

## API

- `pid_alive(pid)` — live non-zombie process. POSIX `kill(0)` plus `ps` state; Windows `OpenProcess`/`STILL_ACTIVE` (not `tasklist` substring). Non-positive PIDs are dead.
- `process_identity(pid) -> {"started", "command"} | None` — kernel birth plus argv. Darwin `proc_pidinfo` + `KERN_PROCARGS2`; Linux `/proc` start ticks + boot id + cmdline; Windows `GetProcessTimes` + image path (unverified on hardware). Missing observation returns `None`. PID reuse cannot match a saved dict.
- `owned_process(argv, *, detach_on_success=False, **stdio)` — starts argv once via remote-dev `OwnedProcess` (POSIX session / Windows Job). Yields the `Popen`. Failure or missing identity at detach stops and reaps the tree. Detach only when a verifiable identity is still observed.

## Callers

ModelScope `_worker_process` uses `owned_process(..., detach_on_success=True)` on every platform. `pid_is_active` uses `pid_alive`. `launch_worker` still persists `{pid, identity}` before leaving the owner.

## Evidence (macOS, acceptance runtime Python)

Real `sleep` child + grandchild, temp dirs only:

- identity includes start time and argv; a later process does not match
- dead PID is not alive and has no identity
- exception inside `owned_process` kills parent and grandchild
- successful detach leaves the tree; missing identity cleans it
- ModelScope failed launch cleans the owned group the same way

Windows Job path is implemented, not run here. Native ModelScope download/verify remains Luna/max.

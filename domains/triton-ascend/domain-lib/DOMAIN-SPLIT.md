# Domain split: Triton does not ship remote-exec

`mindie_exec.py` was a stale snapshot of the plugin remote-execution layer
and has been removed. Do not recopy it here.

Remote admission, Gate claim/finish, job references, artifact push/pull,
and CompletedProcess `ssh_exec` live only in:

`plugins/mindie-agent/domain-lib/mindie_exec.py`

Triton skills that need remote execution must import that plugin module
(the installed `plugins/mindie-agent/domain-lib` on `sys.path`). Native SSH
argv, `ssh_run_bytes`, and stdout tar are not part of the public API.

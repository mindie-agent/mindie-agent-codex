# mindie-agent-codex 0.1.9

Pins remote-dev `4c40cb5` and mindie-diagnostics 0.5.4 (`5ae643e`). Diagnostics 0.5.4 claims the first/free log slot with `O_EXCL` before reader-registry/cursor coordination, fixing the intermittent CI failure that the old real-code setup hit at about 101 ms; full-window reader and pressure semantics are unchanged, with no asynchronous redesign.

Keeps the retained large-log behavior: command and TaskOutput results show a bounded UTF-8 head/tail with an omission note and readable full remote raw-log references plus explicitly labelled local cached slices. Incremental/background pages remain full within their output budget, and cursors never skip unreturned bytes. A failed preview read is marked `preview_degraded` while the real exit/result is preserved.

The merged discovery surface keeps 22 remote + 5 component tools; the Codex host outer deadline is 720s (600s Bash + 60s helper grace + 60s cleanup). Oversized-output tests generate data inside the child to avoid Linux E2BIG / Windows WinError 206.

Mechanism/CI checks do not mean every device auto-updated; release assets/tags are produced by the release process separately.

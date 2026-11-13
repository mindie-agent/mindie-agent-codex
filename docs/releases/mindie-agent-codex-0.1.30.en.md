# mindie-agent-codex 0.1.30: Windows manual entry, error visibility, and dispatch state honesty

[中文](mindie-agent-codex-0.1.30.md)

Fixes headless Windows PowerShell 5.1 (`CREATE_NO_WINDOW`) `IOException` on `[Console]::OutputEncoding`, `\\?\` extended-path interpreter/profile comparisons across `hook_launcher.py`, `entry.py`, and `release_activation.py`, Windows Job Object fallback when outer host jobs forbid `BREAKAWAY_OK`, and explicit Codex Desktop/TUI linked mention (`[$mindie-agent](...)` / `[@mindie-agent](plugin://...)`) and UTF-8 BOM recognition (mindie-agent#335). Ordinary prompts without `/mindie-agent` remain strictly context-free and side-effect-free (P2.1).

`UserPromptSubmit` and `bridge.py activate` / `knowledge_cli.py` / `runtime_call.py` replace generic retry-loop `Inactive` prompts with sanitized, stage-specific diagnostics distinguishing hook failures, read-only sandbox permission blocks, `CODEX_THREAD_ID` session mismatches, and undelivered hooks while keeping independent task execution moving.

Separates `entry.py` role dispatch from `profile-binding` preparation and converts malformed JSON profile fields into `ProfileUnavailable` configuration errors (mindie-agent#341), treats non-blocking `update_lock` contention (`BlockingIOError`) in `update_trigger.py` as normal concurrency rather than a reportable fault (mindie-agent#339), classifies malformed catalog JSON as `release_catalog_invalid`, and restricts `runtime_call.py` `execution=outcome_unconfirmed` to actual mutating RPC execution while surfacing `ServiceHostError` stage and code facts (mindie-agent-codex#85, mindie-agent#320, mindie-agent#321).

# Framework Stability & Persistent Configuration

<a href="framework-stability.zh.md">中文</a> | <b>English</b> · <a href="README.en.md">Documentation Index</a>

This document outlines the everyday usage guarantees, Windows atomic configuration I/O, and reproducible component verification workflow for `mindie-agent-codex`.

---

## 1. Everyday Usage & Persistent Preferences

- **One-Time Choice, Durable Persistence**: Community knowledge sharing and automatic tool fault reporting share a single unified contribution switch in `<profile>/.mindie/mindie-consent.json`. Your unified contribution choice and approved project scope persist across tasks, forks, restarts, plugin updates, and ordinary failures. Legacy knowledge-only `contribute` records lacking the unified consent revision (`choice_revision`) do not automatically gain fault-upload permission on upgrade—they surface `needs_choice` until explicitly re-confirmed once.
- **No Overhead When Sharing Is Off**: With community contribution disabled, the plugin performs no `Stop` transcript collection or indexing model calls, while domain knowledge search (`mindie-knowledge`) and remote Ascend execution (`mindie-remote-dev`) remain ready on demand.
- **Corrupt Config Protection**: Transient service or network hiccups recover automatically via persisted worker state; corrupted configuration files surface an explicit error and never trigger onboarding or overwrite saved settings.

---

## 2. Atomic Configuration Reads & Writes on Windows

- **Delete-Sharing + Native Atomic Rename**: The shared configuration store pairs delete-sharing reads with native Windows atomic rename semantics ([`FILE_RENAME_INFORMATION`](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information)). Concurrent readers always see either the complete previous configuration or the complete updated configuration.
- **Contention Safety**: If an external process holds the file open without delete sharing, the update fails visibly while leaving the existing saved document intact—never entering an infinite retry loop or resetting user preferences.

---

## 3. Reproducible Component Verification & Benchmarks

Component checks use committed parser fixtures and exact Git revisions pinned in `plugins/mindie-agent/pyproject.toml` and `uv.lock`, never touching your production installation:

```sh
export MINDIE_CORE_REPO=/path/to/knowledge
export MINDIE_KIMI_REPO=/path/to/mindie-agent-kimi
uv sync --project plugins/mindie-agent --locked --no-editable
uv run --project plugins/mindie-agent --no-sync python tests/run_ci.py
```

- **Per-Release CPU Microbenchmark**: Each `0.0.2` release runs [`benchmarks/release.json`](../benchmarks/release.json) ([`benchmarks/release.py`](../benchmarks/release.py)) to measure `public-transcript` public-message parsing and hidden-reasoning filtering latency in `us/op`, publishing `benchmark-results.json` and `benchmark-results.md` as a reproducible CPU baseline.
- **Paired Release Ordering**: Publish exact dependency Releases (`mindie-knowledge`, `remote-dev`, `mindie-diagnostics`) before running adapter release checks.
- **Explicit Acceptance Boundaries**: Component CI and CPU microbenchmarks verify locked dependencies, parser fixtures, and cross-platform filesystem contracts; live host acceptance and remote Ascend NPU verification are tracked as separate gates.

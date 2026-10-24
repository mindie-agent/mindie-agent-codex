# mindie-agent-codex 0.1.4

[中文](mindie-agent-codex-0.1.4.md) · **English**

Fix hook trust inheritance for Codex Desktop `0.164.0-alpha.11.1`. Native configuration stores command and plugin-content hashes separately as `trusted_hash` and `trusted_content_hash`, while `hooks/list` returns their combined identity. The updater now reconstructs that approved identity before the existing compare-and-swap, retains compatibility with the older single-field format, and preserves explicit disables or changes to either approved hash.

The five-hook trust transfer was verified against the real native API on the Windows enterprise network using saved prior approvals. The 54 focused hook, updater, and candidate-validation tests passed; CI and the formal Tag workflow provide complete platform contracts and release benchmarks.

This release retains the enterprise Git configuration fix from `0.1.3` and pins `knowledge 0.13.4 / remote-dev 0.10.6 / diagnostics 0.5.2`.

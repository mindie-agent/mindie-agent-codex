# mindie-agent-codex 0.1.24

[中文](mindie-agent-codex-0.1.24.md)

Publishes merged #101 activation-child diagnostics and pins the formal diagnostics 0.5.9 release. Failure records retain the actual exception class chain, owned module/function/line, stack fingerprint, typed errno and failing child package identity, rather than allowing a parent wrapper to replace the original evidence.

The child records its original failure before parent incident reuse; previously released parents still retain the child event. Zero-exit incomplete activations also leave diagnostics while keeping their incomplete result, previous runtime binding and retry semantics. Reporting consent, privacy projection, deduplication and hook trust remain unchanged.

Retains the 0.1.23 Windows native-path-depth packaging repair, remote_probe parameters and interpreter guidance, with published remote-dev 0.11.7 and coordinator 0.7.6. This release does not change the MCP tool catalog or permissions.

This improves future evidence, not the historical trigger in mindie-agent#324. Publication, background discovery, candidate installation, runtime activation and already-loaded host state are separate checks; loaded host scripts require a natural reload.

The formal workflow separately verifies dependency Releases, exact-tag tests, benchmarks, plugin metadata and SHA256SUMS. Original-incident reproduction and NPU acceptance are not claimed.

# mindie-agent-codex 0.1.3

[中文](mindie-agent-codex-0.1.3.md) · **English**

Candidate publication-contract validation now inherits system and user Git configuration, preserving enterprise proxies, CA paths, and Windows schannel trust. It retains the temporary bare repository, disabled Git hooks, and exact publication commit verification.

The plugin pins `mindie-knowledge 0.13.4`, whose independent component builder uses the installer's default system certificate policy. The existing formal `remote-dev 0.10.6` and `mindie-diagnostics 0.5.2` pins remain.

The previous `publication_fetch:fetch_failed` failure was reproduced on a Windows enterprise network; the repaired Git fetch and publication-contract read passed there. The original fix passed Ubuntu, macOS, and Windows CI. This release's workflow verifies its exact dependencies, full component contracts, and benchmarks and publishes the reports as release assets.

Updates continue to prepare runtimes in the background and switch when idle. Saved contribution settings and explicit session knowledge activation retain their existing behavior.

# mindie-agent-codex 0.1.26

[中文](mindie-agent-codex-0.1.26.md)

This maintenance release closes delivery gaps between publication, independent component updates and native plugin activation. Components continue to follow the latest formal release while preserving installed and externally bound standard-component version floors.

- A failed local runtime preparation reads the current binding when rebuilt. A settled CAS rejection that explicitly performed no activation can also prepare again. Unreconciled native installations and activations retain their original immutable attempt and are never blindly replayed.
- Publication validates the native MCP entry's target version, preventing a plugin version bump from shipping stale generated entries. Native entries are regenerated for this release.
- The baseline uses formal component sources. Plugin upgrades continue to inherit independently upgraded components and external standard sim/profiler bindings without downgrades.
- Protected-main dispatch runs the shared publisher. A complete private draft must pass real old-updater generation builds and native-host acceptance before downloaded-byte verification, immutable tag creation and formal publication. Fresh old installations repeat upgrades against the public source afterward.
- Native acceptance starts from formal plugin `0.1.23`, covering independently upgraded components, external interpreter bindings, preservation of the explicitly disabled Hook and fresh native MCP tool discovery. Staged and public-source reports remain distinct.

Host support remains exact `0.153.4` or `>=0.162.0-alpha.2 <=0.999.999`, including prereleases. Future patch, minor and alpha versions within that interval do not require individual allowlist entries. Actual test, benchmark and upgrade conclusions belong to the corresponding CI and shipped evidence; historical performance experiments are not relabeled as new measurements.

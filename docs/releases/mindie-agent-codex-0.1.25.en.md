# mindie-agent-codex 0.1.25

[中文](mindie-agent-codex-0.1.25.md)

Fix Codex Desktop `0.162.0-alpha.2` being excluded by the overly high `alpha.17.2` compatibility floor, which made installation or update report `no_compatible_release`. The release declaration and all 12 consumed interfaces now support exact `0.153.4` or `>=0.162.0-alpha.2 <=0.999.999`, including prereleases.

The forward interval introduced in `0.1.17` remains effective; `0.162.3-alpha.17.2` was already eligible. This patch retains interval selection and adds 45 combinations of later patch/minor versions and alpha/beta/rc/stable versions, checking first installation and selection with an installed plugin. New versions inside the interval need no per-version entry.

Native CI adds official `0.162.0-alpha.2` on Ubuntu and Windows. Isolated Windows acceptance checks installation, five hooks, trust CAS, explicit disable preservation, the locked runtime, SessionStart, discovery of 23 remote tools and six component tools, and component generation build, validation and binding cutover. It does not claim model-turn or NPU workload acceptance.

This release retains the main branch's `0.1.24` hook startup and activation diagnostics improvements and reuses its formal runtime dependencies. This patch does not change component pins, the MCP tool catalog, grants or the historical product channel. The release workflow verifies exact-tag tests, benchmarks, plugin metadata and SHA256SUMS.

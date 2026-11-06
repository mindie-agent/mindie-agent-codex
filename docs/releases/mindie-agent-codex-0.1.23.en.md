# mindie-agent-codex 0.1.23

[简体中文](mindie-agent-codex-0.1.23.md)

Fix Windows upgrades that inherit independently updated components under the default native plugin data directory. A deep generation path combined with setuptools' `build/bdist.../wheel` staging tree can exceed Windows relative-path limits. The old runtime binding remains available when preparation fails.

The plugin now uses Hatchling to package the same Python modules, MCP catalog, hooks, skills, assets, and pinned manifests directly, avoiding that intermediate tree inside the source directory. Native upgrade acceptance uses the actual `CODEX_HOME/plugins/data/mindie-agent-mindie-agent` depth instead of a shorter checkout-relative fixture.

This release includes 0.1.22's four `remote_probe` arguments and shared source/interpreter guidance, and retains formal remote-dev 0.11.7 and coordinator 0.7.6 pins. Plugin lifecycle, MCP deadlines, user grants, component version floors, and failure semantics remain unchanged.

The formal workflow validates release artifacts, exact-tag tests, and benchmarks. Native rebuilds, dynamic-library loading, and A2/A3 NPU canary acceptance are outside this patch's completed validation.

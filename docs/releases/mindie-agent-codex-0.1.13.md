# mindie-agent-codex 0.1.13

This maintenance release lets the native knowledge entry select retrieval from
the next actionable information gap. A self-contained repair with sufficient
code, caller contracts, invariants and a verification path can make zero
knowledge calls. Exact references can be explained directly, and single and
batch explanations share the complete-prerequisite response budget.

The runtime baseline uses knowledge 0.13.6, remote-dev 0.11.1 and diagnostics
0.5.4. Their immutable source commits are recorded in `pyproject.toml` and
`uv.lock`; all three must have formal Releases before this plugin is published.
Knowledge 0.13.5 was an unpublished development identity. Version 0.13.6 adds
the matching release metadata without reassigning that earlier source identity.

| Component | Version | Source commit |
| --- | --- | --- |
| knowledge | 0.13.6 | `9af5a82ad557e2120277693854a0571613c8a115` |
| remote-dev | 0.11.1 | `2a750ec8c74d7f053410d48dd820aeb542e7d3b1` |
| diagnostics | 0.5.4 | `5ae643e19223f2181ecdbaa3edaf464137c4805e` |

Automatic updates include managed components bound through standard
`python -m` commands. Candidate construction preserves installed version
floors, and activation compares the original binding before switching to the
prepared environment. The first upgrade from older snapshots preserves
component commands and current versions. Custom wrappers remain user-owned;
loaded callers may finish in retained old directories. New bindings replace
the old execution target without deleting knowledge data or changing consent.

## Native host compatibility

The compatibility declaration adds the exact installed Desktop CLI version
`0.165.0-alpha.1.2`. Earlier declarations did not cover that version, so their
normal background selector reported `no_compatible_release`.

Release preparation validates the actual executable in an isolated native
profile, without model requests: plugin installation, five hooks, trust
compare-and-swap, explicit disable preservation, locked runtime setup,
SessionStart and MCP discovery. Existing contract tests cover the remaining
registered protocol boundaries. This does not claim a paid native model turn
or NPU acceptance for that host version.

The native and affected contract suite passed 124 tests and 52 subtests in
39.12 seconds on macOS arm64. Actual discovery returned 23 remote tools and
five component tools, with no knowledge schema in the default public catalog.
The test profile was isolated from existing user installations.

An installed binding, a prepared candidate and the catalog loaded by an
already-running host remain separate states. Formal post-release acceptance
records normal background discovery and the active component versions;
already-loaded host definitions follow the native refresh boundary.

## Validation scope

The release workflow runs the declared contract suite and publishes its
benchmark, wheel, source archive, plugin compatibility metadata and checksums
from the exact tag source. Linux, macOS and Windows adapter CI and the existing
native CLI matrix remain required.

The retained DSH and Codex P0 pilot observed lower time and token use in four
simple SFA pairs, with asymmetric temporary-file noise in Codex. Complex-task
overall non-regression did not pass. These historical CPU results are not
relabelled as acceptance of this release source or as NPU performance evidence.

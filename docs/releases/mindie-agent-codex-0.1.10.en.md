# mindie-agent-codex 0.1.10

This release repairs idle upgrades from existing installations. An idle `tools-mcp` no longer holds the update lock; a newly installed MCP launcher can migrate a canonical binding still on 0.1.8 / 0.1.9; interrupted local activation and obsolete candidates whose native cache was evicted no longer permanently block update checks.

## Changes

- The entry releases the shared lock after profile validation and before serving MCP. Actual tool calls retain the lock for their execution and release it on completion.
- A newer installed MCP seed enters its new migration controller, waits for old operations and services to become idle, and publishes the complete interpreter, scripts, and configuration binding through the existing atomic activation path. Hook commands remain stable; Stop never starts migration.
- Activation children receive an immutable attempt snapshot through stdin. Replacing a pending receipt cannot let a delayed old child downgrade a newer canonical binding. Interrupted local activation uses canonical readback; unconfirmed native installation or marketplace writes remain unreplayed.
- Completed prepared candidates can be retired after native cache eviction. Portable errno handling fixes concurrent startup failure caused by macOS file-lock contention.
- The 2760-second MCP startup budget covers baseline preparation, component inheritance, old-call drain, and migration. Native tool-call budgets remain 720 seconds for remote tools and 330 seconds for components, with the existing environment passthrough contract.

## Verification Scope

Final local contracts passed 598 tests and 493 subtests, with 26 skipped and 2 live NPU tests deselected. macOS, Linux, and Windows CI run the component contracts against the same commit. The formal release workflow separately validates dependency Releases, runs contracts and microbenchmarks, and attaches reports and checksums.

A private configuration on actual macOS Codex Desktop `0.164.0-alpha.11.1` started with the real published 0.1.8 runtime and a natively installed 0.1.9 candidate. Native installation of the candidate source and refresh of the same chat completed migration in about 47 seconds: both MCPs loaded 0.1.10, exposing 22 remote tools and 5 component tools; the complete canonical binding switched; the idle exclusive lock was available; user fields and contribution consent were preserved. The normal background loop retired the old candidate and continued Release discovery. Before candidate publication, selection correctly reported no higher compatible formal Release.

This pre-release check uses isolated candidate source and does not prove post-publication background discovery. The administrative `mcpServer/tool/call` API carries no real turn identity; the check confirms rejection without injecting an identity to fake a business call. A non-destructive runtime component CLI status check passed. Real user-turn tool execution and actual user-host loading are separate post-publication checks. Another actual-host probe confirmed that refresh allows an old in-flight call to complete before retiring its connection.

## First Upgrade Boundary

Already-running 0.1.8 / 0.1.9 updaters cannot load the fix themselves. If an updater is stuck on an old activation receipt, first recovery requires the native installer to select this formal Release's exact source, followed by a host-supported MCP refresh or natural reload. Repeating `plugin add` while the marketplace still points to an old candidate installs the old version again. Do not substitute manual version edits, lock deletion, or runtime overwrites for migration.

Read back native installation, runtime preparation, canonical activation, and host loading of new tools separately. The first two do not prove that the upgrade is active. Saved user consent is preserved; knowledge remains gated per session by manual `/mindie-agent` invocation.

## Runtime Dependencies

Pins are unchanged from 0.1.9 and reuse formal component Releases:

| Package | Version | Commit |
| --- | --- | --- |
| remote-dev | 0.10.7 | `4c40cb5d302ddb8400ff6abb7dc47873b931eaa9` |
| mindie-diagnostics | 0.5.4 | `5ae643e19223f2181ecdbaa3edaf464137c4805e` |
| mindie-knowledge | 0.13.4 | `6e921b205c00d592c25f849653cbfb72b73fe318` |

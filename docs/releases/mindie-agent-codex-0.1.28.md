# mindie-agent-codex 0.1.28

The resident MCP facade now emits standard tools/list_changed notifications when
the committed runtime changes. Fresh discovery and calls use the same selected
interpreter and payload; in-flight calls keep their original generation. Tool
catalog refresh never requires knowledge activation or a managed task attachment.

Knowledge activation does not repair native transport identity or managed task
binding. Structured coordinator recovery permits independent environment and
bare-metal lease work without repeated activation commands. Actual resource
ownership, native caller checks and uncertain-write reconciliation remain enforced.

Codex detects and enables the host's mcp_tool_list_changed capability through the
public configuration API. Native acceptance on Codex 0.167.0-alpha.4.1 verified
same-turn descriptor refresh with one tool invocation and no paid model call.
Unsupported hosts and already-loaded legacy facade code follow the native reload
boundary; installation success alone is not a host-loaded catalog receipt.

The release publisher automatically creates and validates mindie-plugin-release.json
for old updaters before checksums and staged/public upgrade acceptance.

# mindie-agent-codex 0.1.7

Ordinary Codex sessions no longer discover knowledge tool names, descriptions or schemas through MCP. After explicit `/mindie-agent` activation, the Skill loads the private catalog and calls the same engine under the current session grant. Another chat's activation never changes the shared catalog.

The new `mindie-tools` MCP exposes profiler, sim, coordinator, local diagnostics status and support-bundle export. The first three use independently installed component CLIs, optionally selected through `component_commands`; missing installations fail explicitly. Remote development remains available by default. The human-facing npu-top dashboard is excluded.

Execution reuses bounded output, deadlines, cancellation and process-tree cleanup while preserving exit codes and original output. Contract coverage exercises wire discovery, cross-chat isolation, private knowledge queries and real CLI success/failure. The release also includes exact-tag contract tests and the public-transcript CPU microbenchmark; these are separate from NPU performance and client auto-update acceptance.

# Domain tooling

There are no bundled per-task domain skills in this release. The old domain
skill directories were removed outright; useful capabilities return later
through the community publishing path as reviewed Skills. The deferred
profiling-analysis skill remains in place and is not part of this routing.

For NPU work after activation, use the plugin's `mindie-remote-dev` MCP tools
directly with the actual host, port, user, container and working directory
supplied or verified for the task. Shared CLI helpers under
`plugins/mindie-agent/domain-lib/` bind the user's current business directory
and take explicit remote targets; managed execution goes through the
configured coordinator. Poll owned jobs and retrieve their artifacts through
remote-dev; reuse the returned full container ID.

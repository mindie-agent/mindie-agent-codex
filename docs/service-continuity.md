# Installed service identity

Native Codex owns plugin distribution and updates. uv prepares the complete
locked, noneditable runtime. MindIE has no code-update scheduler, mutable
current-generation registry, native transaction recovery manager, version
ordering or service-successor chain.

A prepared release entry points to one installed interpreter and package outside
the native disposable cache. The existing user adapter configuration is the only
installation binding. SessionStart and MCP can initialize an empty profile;
ordinary entries cannot replace a different binding. Stop cannot download or
prepare a runtime. Warm entries do not hash the disposable native source.
Explicit preparation rejects different bytes under an already prepared release
identifier; a warm entry continues its original fixed runtime, never activates
an illegal same-version replacement.

The shared core authenticates the exact engine configuration and owns one service.
A ready service may be reused without resupplying provider environment secrets.
A cold start requires the selected provider context and fails explicitly when it
is missing. A long-lived MCP process freezes its installation identity in memory
and checks the user configuration under the existing profile lock before each
request. A changed binding produces `reload_required`, without dispatching a
request or selecting another runtime. There is no promise of transparent
cross-version execution by old loaded sessions.

Authorization is separate from code identity. Current explicit consent, scope,
disabled/reporting choices, full materials, state FORMAT, usage and uncertain
operation receipts remain authoritative user data. Code reload does not ask for
consent again. This unreleased version drops development-generation migration
and consent inference; unsupported layouts are rejected without modifying or
removing their existing files.

The model-free cache experiment established why fixed entries are necessary:
Codex 0.153.4 evicts the prior cache after a marketplace update while the existing
app-server still holds old commands. Native SessionStart, MCP and late
UserPromptSubmit control then ran from fixed entries with zero external provider
requests. This is path-lifetime evidence, not natural Stop collection acceptance.

Failure projection is also native: fixed `systemMessage` warning JSON is observed
in `hook/completed` for SessionStart and Stop on 0.153.4 with a synthetic loopback
response. It grants no continuation, retry or tool decision. Exact alpha source
confirms the same Stop warning parser; Windows native observation remains a
separate gate. Full-body capture, index/usage and cleanup acceptance is recorded
against the final released runtime separately from these component controls.

# Installed service identity

Native Codex owns plugin distribution and installed-state facts. uv prepares the
complete locked, noneditable runtime. The shared Release selector chooses the
highest compatible ordinary Release; the adapter projects it into the native
installer. There is no code-update daemon, current-generation registry or
service-successor chain.

A prepared release entry points to one installed interpreter and package outside
the native disposable cache. The existing user adapter configuration is the only
installation binding. SessionStart and MCP can initialize an empty profile;
ordinary entries never choose an update. Stop cannot download or
prepare a runtime. Warm entries do not hash the disposable native source.
Explicit preparation rejects different bytes under an already prepared release
identifier. A warm facade dispatches the complete interpreter/helper pair from
the canonical profile; it cannot select its own older version or activate an
illegal same-version replacement.

The shared core authenticates the exact engine configuration and owns one service.
A ready service may be reused without resupplying provider environment secrets.
A cold start requires the selected provider context and fails explicitly when it
is missing. A long-lived MCP process freezes the profile identity and reads its
complete runtime binding under the existing shared operation lock before each
request. This keeps in-flight operations on one runtime. Background preparation
does not hold that lock. Activation takes the exclusive lock only when idle,
retires the exact previous owner, observes the new owner's readiness, then
publishes the canonical binding atomically. Busy operation/service state defers
activation. New calls during that short gate return explicit temporary
unavailability before execution; ordinary completion never requests a reload.
Failed/uncertain handoff retains known effects and does not blindly restore or
start another owner.

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

Hook command text is also independent of the plugin version. A running
app-server can retain old plugin declarations after an external native update;
changing the command and its trusted hash made a new thread silently omit
SessionStart/Stop in the isolated two-runtime experiment. The Hook facade reads
the canonical profile, checks its exact prepared runtime receipt, and executes
that immutable entry. Only an absent profile may bootstrap from the native
`PLUGIN_ROOT`; a missing or mismatched installed entry is an error. No new
selector or trust authority is introduced. Native version/cache changes with
identical command text retained the same trusted hashes in the model-free
0.153.4 control; full natural capture remains a separate acceptance gate.

Failure projection is also native: fixed `systemMessage` warning JSON is observed
in `hook/completed` for SessionStart and Stop on 0.153.4 with a synthetic loopback
response. It grants no continuation, retry or tool decision. Exact alpha source
confirms the same Stop warning parser; Windows native observation remains a
separate gate. Full-body capture, index/usage and cleanup acceptance is recorded
against the final released runtime separately from these component controls.

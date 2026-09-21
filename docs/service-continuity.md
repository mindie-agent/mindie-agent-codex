# Service continuity during plugin updates

An updater used to stop an idle knowledge service and install the next plugin
without restoring the service. A later query hid the defect by starting it;
a task that only used remote development could then deliver a Stop event to
no service and lose that increment.

The update transaction now confirms that the exact old loopback endpoint has
exited, switches the native package and committed runtime tuple, and makes
one bounded restoration attempt while retaining the exclusive operation lock.
Restoration uses the selected interpreter and engine configuration directly,
never a shared-lock launcher. Core startup remains one spawn, five seconds,
and three readiness probes; the returned service must be unfrozen.

Only a service stopped by this updater invocation is eligible. An absent
service stays absent; revoked or circuit-paused task leases do not start a
service. Stop hooks still only notify an existing service. No task is activated,
no transcript is replayed, and no model call belongs to the updater.

Rollback restores a service only after both the retained native package and
the exact old pointer are proven restored. Failed or interrupted handoff is
kept in the existing updater status as service_handoff. Same-revision checks
and successful Feed sync cannot hide it. A later scheduler invocation does
not interpret that marker as permission to spawn or retry restoration.

The selected native profile is retained for the replacement service. Claude
Code imports only supported provider/model/effort values from that profile's
user settings into its isolated organizer; it does not import hooks, tools,
MCP configuration or project settings. Secret values never enter updater status.

## Validation boundary

Real local-daemon acceptance covers live, absent, revoked and rollback paths
for Kimi and Claude Code. Those cases use a controlled native-install boundary;
they establish actual process/endpoint/admission behavior, not native package
installation or model authentication. Native acceptance is recorded separately.
Windows real-machine acceptance remains with the maintainer after merge.

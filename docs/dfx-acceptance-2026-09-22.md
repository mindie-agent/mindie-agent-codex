# Fault diagnostics — 2026-09-22

Own-tool failures now preserve the original result and attach a bounded local
incident. Trusted inner references pass through instead of creating duplicate
reports. Reporting is independently opt-in; status is read-only and install
leaves uploads off. The startup fallback is copied verbatim from the shared
package and never installs dependencies or uploads. Existing updater checks run
offline maintenance, preserve degraded results and do not replay business work.

A real Codex `gpt-5.6-luna` / `max` task invoked the candidate MCP once.
An isolated helper returned invalid JSON before any SSH. The model received
`helper_response` / `helper_protocol`, an incident matching the only local log,
and an unconfirmed outcome; it did not retry. No knowledge admission or shell
bypass occurred. A later controlled reporting process created diagnostic Issue 8;
readback, closure and reconciliation to the closed Issue succeeded.

The native task used candidate scripts, not a claim of new Stop delivery or
final production package activation. The first reporter readback exposed a
zero placeholder process ID; it was fixed and later real records verified the
writer ID. Reporting-status wording was clarified after native observations.
The changed bridge may require the user to trust the updated native Stop Hook.

Shared actual log, transport, GitHub and macOS service evidence: [diagnostics acceptance](https://github.com/mindie-agent/diagnostics/blob/main/docs/dfx-acceptance-2026-09-22.md). Windows hardware remains unverified.

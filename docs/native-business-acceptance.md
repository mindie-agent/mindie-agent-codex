# Native business acceptance

Component CI and a native business run answer different questions. A green
installation, a saved preference, a returned tool definition or an agent's final
text cannot substitute for an observed business outcome.

## Ordinary CI

Run `python tests/preflight.py` with the documented exact core/peer checkouts,
then `python tests/run_ci.py`. Every discovered component case runs once.
Runtime compatibility, Windows ownership, encoding, public service lifetime,
configuration outcomes, model selection and native-shell Stop tests run first.
The remaining modules reuse that same suite instead of repeating the early
modules in a second discovery pass. A failed boundary stops the run early.

| Independent boundary | Observable invariant |
| --- | --- |
| Setup | Failed config writes do not save completion; existing task binding gains capture after successful config |
| Body and metadata | Public body survives without any model; optional metadata uses an independent model/effort and cannot change content |
| Windows shell | Both CMD and PowerShell deliver the original Stop stdin once; child failure remains benign. The 5 s host watchdog includes cold shell/interpreter launch; bridge work remains bounded to 1.3 s on Windows, independent of transcript size |
| Process lifetime | Entry helper has exited before service readiness is checked; cold Stop is consumed after its helper exits |
| Process ownership | Ordinary descendants die at completion/timeout even in a service-capable launcher |
| Update | Actual native inventory shape is accepted only for the owned marketplace; changed hook dependencies invalidate reuse |
| Core loop | Persistent attempt/region/apply/outbox receipts distinguish success, coverage gaps, temporary environment failure and uncertain write |

These checks use real files, processes, locks and database transitions at the
boundary under test. A summary model double or file-backed GitHub transport
is a component fixture, not native-model or public-publication evidence.

## Controlled native run

Use one isolated native profile per OS, an exact committed adapter/core pair,
the approved contribution target/account/scope and native hook trust. The
configured service and its updater must inherit the same executable environment;
check local `publication_runtime` prerequisites without exposing credentials.
Run a single bounded real business task with the requested native model and
effort. For an NPU task, retain the actual command, selected device mapping,
outputs, exit code and post-run process release. Preserve failed attempts too.

Let the native turn end normally. Do not manually inject Stop, write capture
rows or use contribution-retry as evidence of automatic publication. Inspect:

1. The native session/turn has a corresponding real capture and bounded regions.
2. A service remains alive after the caller exits; body capture makes zero
   model calls. Optional summary configuration is independent of the requested
   business model. Do not inspect or export hidden reasoning.
3. Compare draft content to the filtered, redacted public messages. It must
   preserve their order and text, contain the business result and exclude all
   tool records. Public assistant claims remain claims, not tool evidence.
4. The automatic outbox receipt has the expected PR and head; verify the actual
   remote head and content. An explicit operator recovery is labeled separately.
5. A new native task retrieves and reads the actual entry. Local draft reuse and
   public-feed retrieval after merge are different results; record which ran.

Never mark the whole transcript covered while a failed region, clipped field or
unprocessed continuation remains. A later successful task does not erase that
history. Legacy organizer gaps stay visible across migration. Do not reset
cursors, counters or model settings to manufacture a pass.

Hosted CI has no authorized native model credentials, public contribution scope
or NPU reservation. Those are the prerequisites for this controlled run, not a
reason to skip cheap public-boundary checks or report component CI as end-to-end
acceptance. Reuse valid business evidence after unrelated component-only edits;
repeat the native boundary that changed and record exact start/update commits.

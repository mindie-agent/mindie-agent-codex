# mindie-agent-codex 0.1.6

Updater product faults now enter the shared diagnostics recorder instead of being omitted or unconditionally marked non-reportable. The native updater keeps its original failure, retry and uncertain-write semantics.

The baseline pins mindie-diagnostics 0.5.3, including typed classification and local reporting eligibility. Reviewed direct pins override older transitive diagnostics pins, using the existing package manager rather than a second updater. Network, credentials, cancellation and local prerequisites remain visible locally.

Tests cover fault recording and ingestion without publishing test issues. Existing profile contribution consent is required; unconsented history is never backfilled. Local component tests do not certify automatic activation on every remote device.

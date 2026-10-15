# Native distribution and fixed runtime review

Date: 2026-10-15. Base: `cf9a90a3a41df01caaa3ae63090acec67713a953`.
Principles: design commit `f113b376ac40b957dd6f17dbbbd676e5d3bb9485`,
document blob `4a4c792ec5113e7e7f77f95ed9cdb3cc762d1e61`.

## Result and boundaries

Native Codex owns Git marketplace installation, update selection and removal.
uv owns the complete dependency lock, Python acquisition and noneditable
installation. The adapter retains only immutable runtime preparation, native
identity/protocol translation and exact installation validation. The existing
adapter configuration is the single installation binding.

The old code-update manager, scheduler launcher, runtime selector, service
handoff layer, generation leases and copied consent store are removed.
Unreleased legacy migration, version routing and successor chains are not kept.
The refactor reduces production Python from 12,099 to 8,905 lines and test Python
from 13,629 to 10,587 lines, including newly added files. The complete generated
uv lock is counted separately from handwritten code.

Ordinary SessionStart and MCP initialization can prepare an empty profile.
An existing different installation is rejected without rewriting its data.
Loaded MCP checks its frozen installation identity before every request.
This change does not implement upgrade to a different binding or promise
transparent old-session execution across versions.

Contribution scope, disabled state and reporting choice remain persistent user
data, independent of code identity. A second adapter installation reuses valid
canonical shared settings and consent without rewriting them. A damaged,
unsupported or missing authority is not permission, first use or user disablement.
Paid/unknown receipts and released state FORMAT protection remain intact.

## Nine-principle review

1. **Total task cost:** remove a second release resolver and native mutation
   transaction manager. Warm entries neither scan release directories nor hash
   disposable native caches. Full source/lock validation belongs to preparation.
2. **Closed operations:** runtime preparation is exactly one locked uv sync,
   installed-byte readback and candidate-owned validation. It does not decide
   whether an old business operation should be repeated.
3. **Comprehension:** native plugin commands remain the distribution interface;
   there are no generation or successor controls. A changed loaded binding says
   reload is required; an unsupported development replacement says it is
   unsupported rather than implying reload repairs it.
4. **Responsibility:** native host selects plugin bytes, uv installs dependencies,
   the adapter binds Codex identity, and the shared core owns service/capture
   state. Existing profile and preparation locks protect their respective
   operations; there is no new database, selector or schedule.
5. **Informational Skill:** the entry Skill still explains optional capabilities.
   It does not require activation, a status ritual or a closing contribution.
6. **Reference knowledge:** full mechanically redacted bodies, reference hashes,
   feedback and public-content contracts are unchanged. Neither successful
   installation nor a test gives retrieved material decision authority.
7. **On-demand behavior:** no model call occurs in installation, lifecycle probes
   or dependency checks. Stop cannot acquire Python or install a runtime. Feed
   refresh uses SessionStart/explicit sync and existing shared interval/backoff.
8. **Reuse:** actual native cache-lifetime and cold-start probes establish the
   narrow need for a stable release entry and a declared startup window. Existing
   owned-process/Job handling contains uv; no second guardian was introduced.
   Deleted mechanisms are not retained behind a compatibility wrapper.
9. **Observable failure:** fixed native `systemMessage` warnings expose failed
   SessionStart/Stop while granting no continuation. Failed uv output stays in
   a private log with its stage; no ready receipt is published. Candidate Git
   validation retains bounded execution facts without raw stderr/argv. Consent,
   configuration, cleanup and business outcomes remain distinct.

Review fixed concrete defects: hidden SessionStart/Stop failure projection;
raw installer subprocess ownership; initial-entry-to-MCP identity race;
configure swallowing a damaged engine and rewriting retired fields; component
release-version sorting instead of FORMAT validation; and refusal to reuse the
other adapter's valid shared authorization.

## Validation and evidence limits

The official lock fixes knowledge
`6a4bbf824820b9a72bb81de71034d11a8596e697`, remote-dev
`dc3f7cecdd9cabbc3cc222f576725d69514cfdf7` and transitive diagnostics
`10cc3391ed358d7b7e0e1acb99ca5be4d1934b47`. The lock resolves 172 packages;
the macOS Python 3.13 environment installs 161 compatible packages.

- The 53-case boundary selection passed, including real SIGTERM and SIGKILL
  bootstrap-owner death with a synthetic installer and build descendant.
  Readiness handshakes precede cancellation; both OS locks release before test
  cleanup, and no runtime/entry readiness receipt appears.
- The 32-case setup/runtime selection passed after shared-profile reuse was
  added. Malformed authority and retired engine fields preserve existing bytes.
- The 28-case remote/entry selection passed after obsolete test-only missing
  runtime-path and inferred-consent fixtures were made explicit.
- The complete source suite then passed 409 tests in 115.637 seconds with 17
  platform skips. An earlier complete run found the obsolete fixture gaps; its
  failed log is retained and is not counted as passing.
- A final 14-case setup selection passed after allowing the other adapter's
  valid disabled, not-yet-authorized shared document. No consent is created;
  enabled/off/reused-choice and next-action output match the preserved state.
- Native macOS 0.153.4 observed actual SessionStart and Stop warning events
  using one local synthetic response and zero external provider/model requests.
  This proves warning projection, not natural capture or model benefit.
- Earlier native Git cache eviction and installed-wheel controls explain the
  entry design. Their source manifests differ from this final refactor and do
  not establish final installed bytes.

Final installed-package readback and same-version first/increment natural Stop
acceptance on macOS, native Windows and WSL remain separately recorded gates.
They must verify body/index/usage, host-exit consumption, harmless remote MCP,
disable/cancellation and keep-data uninstall. Component tests, cache controls,
local Git delivery and source review do not substitute for those native gates
or public GitHub delivery. Benchmark work remains a separate task.

# Test boundaries and cost

[中文](testing-contracts.zh.md) | English · [Documentation Index](README.en.md)

Run `tests/preflight.py` with the exact checkout paths documented in the README,
then use the standard unittest runner. CI compiles every Python file first,
checks the installed Git pins, runs the unmodified setup/update probe, and
executes native ownership, Unicode and service-lifetime contracts before the
full component suite. No additional test runner or self-reported coverage
registry is required.

| Boundary | Evidence | What it does not establish |
| --- | --- | --- |
| Installed dependency | Both production probes run without adding attributes; missing consumed methods are negative controls | Native model behavior |
| Publication compatibility | Actual packages produced by the runtime pin pass the independent exact validator selected by the product-pinned declaration; changed bodies, declaration bytes and unsupported formats are rejected | Native model accuracy or Bot authorization |
| Windows ownership | Real child processes, inherited pipes, exited leader, timeout and normal return; external watchdog | POSIX process-group behavior |
| Consent serialization | Two writers contend on the real OS lock, with observable acquisition barriers | A mocked lock ordering |
| Service handoff | Authenticated idle acknowledgement, released consumer lock, natural exit and restart | Idle acknowledgement alone |
| UTF-8 | Real setup with Unicode paths, core readback, MCP round-trip under cp1252 | Only ASCII installations |
| Durable replay history | Batch-seeded SQLite history and real API calls across the former 4096 boundary, oldest/newest replay rejection after reopening | Exhaustive mutation coverage |
| Client acceptance | Native installed plugin selection and actual model/tool events | A manifest parse or inventory receipt alone |

Use fixtures at the smallest relevant external boundary. A Python fixture is a
script argument to a real interpreter, never a fake executable interpreter.
Tests may replace a model or remote helper when that external behavior is not
under test. They must not patch missing production APIs into a positive probe,
force-kill a service and count that as successful shutdown, or treat missing
dependencies as passing skips.

Runtime code, material formats and trusted publication validation are independent
boundaries. The requirements file binds installed runtime code. The product's
verified publication commit and declaration digest bind the public formats,
review contract and exact trusted Bot validator. The runtime parser must accept
those formats; its Git commit need not equal the Bot validator commit. Changing
runtime implementation alone therefore does not require changing Bot code or
the public declaration. A material/review contract change still needs an
explicit compatible product/publication combination.

`test_publication_compatibility.py` reads that fixed official declaration and
normally installs its exact validator into a separate interpreter, including
normal dependencies and VCS receipts. Tests never execute publication content.
An explicitly supplied `MINDIE_VALIDATOR_PYTHON` can reuse that interpreter only
after its exact installed pin and dependencies pass validation; invalid reuse
fails. This is component evidence for package interoperability, separate from
client hook, model, hardware and unattended Bot acceptance.

The full suite keeps the host's default text encoding. UTF-8 protocol fixtures
write bytes like the production entry point; source fixture edits explicitly
read and write UTF-8. Pin comparisons retain the exact canonical Git blob hash
and VCS commit receipt, while source comparisons normalize only Git's CRLF
checkout transformation. This permits Windows checkouts without ignoring any
source content differences.

The history test prepares 4093 completed rows in one transaction instead of
repeating 8186 redundant claim/finish calls. Seven actual claim/finish
pairs still exercise initial creation and crossing the former limit. This is a
white-box fixture for a specific state boundary, not a throughput benchmark.

Platform-specific tests stay explicit. Windows Job Objects, POSIX groups,
permission mechanisms, and FIFO behavior are different mechanisms; a skip for
one mechanism is not proof that the other passed. Native model, scheduler and
hook acceptance remains separate from component CI. Small early gates are
repeated by full discovery where applicable so ordinary unittest discovery
remains complete; full suites run once per CI platform. Pull requests do not
also trigger an identical branch-push run.

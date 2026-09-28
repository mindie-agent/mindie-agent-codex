# Test boundaries and cost

Run `tests/preflight.py` with the exact checkout paths documented in the README,
then use the standard unittest runner. CI compiles every Python file first,
checks the installed Git pins, runs the unmodified setup/update probe, and
executes native ownership, Unicode and service-lifetime contracts before the
full component suite. No additional test runner or self-reported coverage
registry is required.

| Boundary | Evidence | What it does not establish |
| --- | --- | --- |
| Installed dependency | Both production probes run without adding attributes; missing consumed methods are negative controls | Native model behavior |
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

The history test prepares 4093 completed rows in one transaction instead of
repeating 8198 redundant claim/finish transactions. Seven actual claim/finish
pairs still exercise initial creation and crossing the former limit. This is a
white-box fixture for a specific state boundary, not a throughput benchmark.

Platform-specific tests stay explicit. Windows Job Objects, POSIX groups,
permission mechanisms, and FIFO behavior are different mechanisms; a skip for
one mechanism is not proof that the other passed. Native model, scheduler and
hook acceptance remains separate from component CI. Small early gates are
repeated by full discovery where applicable so ordinary unittest discovery
remains complete; full suites run once per CI platform. Pull requests do not
also trigger an identical branch-push run.

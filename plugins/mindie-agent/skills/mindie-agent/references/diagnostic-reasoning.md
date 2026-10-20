# Diagnosis, exploration and reusable decisions

Read when a routine path no longer explains the evidence, accuracy depends on
state or observation, or a milestone may revise existing knowledge. These are
decision aids, not a mandatory form or a fixed sequence of gates.

## Choose a discriminating next step

State the unresolved question, plausible competing explanations, and the
observable that would separate them. Choose the least costly useful action:
source inspection, a boundary probe, a minimal reproducer, or a controlled run.
An expensive version bisect is useful only when its cost and attribution are
appropriate. A full test still failing can be progress if the first divergence,
an excluded cause, or a narrower condition is now known.

Repeated attempts with no new fact are a signal to reconsider the method.
Two attempts may prompt a review; they do not force escalation or forbid an
earlier useful experiment. Repair a broken reproduction entrypoint, compile
setup, or measurement before judging the domain hypothesis. Polling one run
does not create a new experiment.

Knowledge tiers describe use: L1 constraints/measurements, L2 procedures,
L3 decision branches, L4 transformations. Reuse, diagnose and explore are modes
of the current subproblem; exploration can produce knowledge at any tier.

## Find the first equivalent boundary that diverges

Align logical inputs, tokens, states, stages and ranks before comparing values.
Different physical block IDs need not mean different logical state. Follow the
producer, layout/mapping, copy, consumer and reduction/nonlinear operation.
Check shape, dtype, stride, ownership, readiness and lifetime at the boundary
that first diverges. A matching collective result does not validate the order
of downstream operations.

For performance, perform Phase 0 upfront subgraph fusion analysis before tuning
an isolated operator's microstages: identify fusable producer-consumer links
(eliminating GM round-trips, standalone `pack`/`cast`/`quant` helpers) and hard
non-fusable boundaries (collective barriers, `L1`/`UB` residency limits, full-reduction
normalization), and check exposed critical-path overlap (`max(P+C, Q+G)`). Then
connect stage service demand, dependencies, queues, last readers and buffer reuse
to the critical path. Missing durations stay unknown. Hardware lower bounds are
not attainable latency predictions, and isolated kernel savings cannot simply be
added into an end-to-end gain. Use the pre-built qualification gate ladder
(`G0-G3`) when available rather than ad-hoc build/timing scripts.

## Treat observation as a possible intervention

Dump, clone, isfinite, tracing and synchronization may change scheduling or
lifetime. If observation removes the failure, distinguish the original result
from the instrumented result. As needed, compare an uninstrumented baseline,
a targeted intervention, an observer-only control and a repeated baseline.
Select controls for the competing explanations; do not run a ritual matrix.

Verify that the proposed fix's path actually executed. Improvement with a zero
branch count is not evidence for that fix. Keep causality inconclusive when
controls do not separate the explanations. Verify the final repair with
diagnostic instrumentation disabled, then check the relevant wider workload.

## Include history and an independent oracle

A stateful reproducer consists of input, initial state and request/scheduling
sequence. Compare fresh and history-exposed processes, check what reset clears,
and minimize the first healthy-to-bad transition. Preserve concurrency when it
is a necessary condition; single-request success does not prove concurrent
correctness.

Choose a reference that does not share the suspected defect. Agreement between
two broken paths, 100% acceptance, service health or exit zero does not establish
semantic correctness. Quantized implementation comparisons need equivalent
quantized inputs and an explicit output/scale/layout contract. Keep operator,
module, first-token, continued-decode and concurrent acceptance scopes distinct.
OracleFreezer protects the chosen probes; it does not prove their independence
or completeness.

## Use actual resources and native context capabilities

Use `npu_count=0` only after confirming the step needs no device. A job called
"compile" can still perform device discovery, JIT or warmup. Device work uses
the existing coordinator lease; timing/profiling isolates the actually assigned
physical devices and shared package siblings, with no fixed device index.
Clean up only the task's owned processes.

Keep goals, hard constraints, current facts, valid counterevidence, unresolved
questions and existing run IDs when reconsidering a plan. A native independent
context, when available and authorized, can initially omit old L3 recommendations.
A full-history fork or filtering L3 out of a Python object does not erase prior
messages. If isolation is unavailable, explicitly treat this as a strategy
change in the current context. Do not invent a second agent runtime.

## Preserve decisions, corrections and limits

Judge evidence validity, goal attainment and reusable information separately.
A valid subtarget gain is not a refutation. A failed candidate need not refute
its mechanism; infrastructure failure or contaminated measurement is
inconclusive. Apply performance measurement and attribution checks when making
performance claims. An L4 mutator still requires the runtime's baseline-bound,
verified final-combination ablation receipt; a diagnostic L2/L3 finding does not.

At a useful milestone, select evidence showing the facts available at the time,
competing explanations, the chosen action and expected observation, the actual
result and changed judgment. Keep source locators and run references. Preserve
human corrections, superseded plans, unresolved alternatives and the conditions
under which a hypothesis was excluded. A human correction changes the active
task constraint; a technical claim still needs evidence. Changed conditions or
new evidence can reopen an excluded hypothesis.

Use `task_state="verified_milestone"` for a finished finding inside an ongoing
task, or `"completed"` for a completed task. `in_flight` remains a no-op and Stop
is not completion. Prefer revision/merge or no-op against related units; keep
supported older branches and rare conditions. Do not replace an old unit from
an incomplete excerpt. Do not add one knowledge card for every failed attempt,
invent missing causal reasoning, or count a recap as an independent replication.

For long tasks, the existing progress record should retain effective
constraints, superseded plans, scoped exclusions, open questions, the next
discriminating action and in-flight run references. On recovery, query those
runs before submitting another. Simple tasks need no extra ledger ceremony.

Design provenance: `mindie-agent/docs/knowledge-context-and-exploration.md`,
section 15, mechanisms M1-M6 and transcript sources T1-T5. Those historical
cases motivate the mechanisms; their hardware parameters and specific fixes
are not defaults for a new task.

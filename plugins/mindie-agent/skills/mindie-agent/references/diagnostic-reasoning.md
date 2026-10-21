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

For cross-instance or prefill/decode (PD) disaggregated handoff, do not trust
lifecycle hook names (`wait_for_layer_load`, `start_load_kv`) or scheduler token
counters (`num_computed_tokens` while waiting for remote KV) as proof that data
arrived. Dump consumer tensors only after the layer's actual wait and state
relocation finish and right before the first consuming operator. When checking
whether a transfer missed bytes or used the wrong stride (`(N-1)*stride + valid_bytes`),
pre-fill exclusively owned destination blocks with a dtype-safe sentinel (`NaN`
for floating-point, extreme finite values for quantized caches) before the load,
and never poison shared prefix-cache blocks or page-shared sibling views
(`layer_stride == 0`).

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
semantic correctness. In disaggregated serving, an HTTP 200 response can hide a
failed remote KV pull that silently fell back to local prefill recomputation;
always verify non-zero transferred KV bytes and zero recompute fallback alongside
output morphology (`repeated_ngram`, `punctuation_run`, `immediate_eos`). When a
runtime feature (`batch_invariant`, `enable_fused_mc2`, prefix cache, `ACLGraph`,
`MTP`, or `PCP/DCP`) is active, inspect the effective post-init config and run a
`2×2` `(Colocated vs Disaggregated) × (Feature Off vs On)` check before blaming
the transfer path. Quantized implementation comparisons need equivalent
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

## Scale verification, baseline parity and PR delivery workflows

1. **Progressive verification ladder & pruned-vs-full weight boundary (`W1`)**:
   - Advance strictly through `Single-Op → 1-Node Pruned Weights / 2-Node Minimal Topology → 4-Node Full Weights → Immediate Cluster Release`.
   - On 1-node pruned weights (e.g., 16 experts), do **not** judge long-text semantic quality, but mandatorily verify: (a) operator/weight loading, (b) KV Cache grouping and `build()` count, (c) minimal 2-bucket `ACLGraph` capture covering boundary shapes (never compile all buckets during debugging), (d) non-zero active execution and acceptance rate on quantized/speculative paths, and (e) Peak Activation memory parity against the baseline (`fix hangs/crashes on 1–2 nodes → verify memory activation on 1 node → run full accuracy and SLA sweep on 4 nodes`).
   - On 4-node full weights, run all known historical failure cases first, then benchmark datasets (e.g., GPQA), speculative acceptance rate, and the SLA sweep, and release the multi-node cluster immediately after summarizing. Downscale 4-node or long-context bugs to 1–2 nodes whenever reproducible before instrumenting.
2. **Same-topology baseline A/B diff & three-axis migration parity (`W2`)**:
   - Lock the last known-good baseline anchor first (verified branch or pre-regression PR commit). When operator code is unchanged from the verified baseline, freeze operator suspicion and diff framework glue (Runner, Metadata Builder, KV Cache Specs, Weight Loader).
   - Compare old vs. new side-by-side on dual nodes or single-node split cards (`0..7` vs. `8..15` with identical `TP` and `block_size`) on the same weights, instrumenting key module boundaries in one pass rather than micro-managing debug dump switches.
   - Require three-axis parity before declaring migration complete: **(a) numerical/case accuracy**, **(b) Peak Activation memory** (e.g., catching `SP` / `flashcomm` regressions), and **(c) speculative acceptance rate**.
3. **Upstream/sibling-first implementation & pre-review panorama audit (`W3`)**:
   - Before coding, check whether upstream `vllm` already provides native hooks (`process_weights_after_loading`, native custom ops) and drop obsolete patches; mirror mature sibling patterns in `vllm-ascend` (e.g., placing `FusedInferAttentionScoreV2SinkMetadata` in `AttentionMetadataBuilder` like `DSA V1`, or aligning GQA DSpark with GLM); inherit `common` base classes rather than cross-vendor `ROCm`/`CUDA` runners that require reverse monkey-patching.
   - Before entering review or after a large rebase, perform a file-by-file, line-by-line panorama audit (`before → after → why`) to remove debug remnants, dead code, `NotImplementedError` defensive guards, and string-grep filler UTs, and check cross-model blast radius on shared core files (`kv_cache_interface.py`, `model_runner`).
4. **Speculative graph alignment, out-of-graph metadata & SLA sweep (`W4`)**:
   - For `K`-step speculative decoding (DSpark/MTP), align `ACLGraph` capture buckets to multiples of `K+1` (or the `TP`-compatible step), select initial graphs by padded `num_input_tokens`, and keep fixed-buffer allocation and Tiling/Sink metadata construction in `AttentionMetadataBuilder` / `ModelRunner` outside the captured graph while eliminating `seq_lens` and Mamba `num_accepted_tokens` Host D2H syncs. Validate `greedy` before `probabilistic` sampling.
   - Run SLA sweeps on ~95% prefix-cache datasets with fixed `1K` output, power-of-2 inputs (`2K → 256K`), and per-DP concurrency `1, 2, 4...` under `TPOT <= 30ms`, reporting `TPOT`, `ITL (p50/p90)`, and `mean_acceptance_length` together (plus matched-acceptance `ITL` delta for pure system/D2H optimizations).
5. **Parent + atomic child PR stack, `< 5 min` reduced-weight CI & lightweight pre-flight (`W5–W6`)**:
   - For large full-stack features, maintain **1 parent integration PR** (full stack + deployment doc for multi-node E2E verification) and split orthogonal features into an ordered stack of **atomic child PRs**; keep PR descriptions focused on current responsibilities, post separate clean benchmark tables + desensitized launch commands per weight variant (no internal hashes, artifact paths, or false-positive notes), triage all non-AI human review comments with regression checks, and build single-file layer/expert-reduced or dummy-weight E2E CI smoke tests finishing in `< 5 min`.
   - Use a lightweight 4-point pre-flight check instead of heavy hash-gate runbooks: (a) parallel C++ builds (`-j` / `MAX_JOBS`) + `ASCEND_CUSTOM_OPP_PATH` `.so` check, (b) explicit `VLLM_VERSION` pinning, (c) `GLOO/HCCL_SOCKET_IFNAME`, and (d) running benchmark clients inside the remote server container rather than over VPN.
6. **Custom-op ABI/counting-domain, runtime true-vs-false hang, zero-NPU checkpoint preflight & hybrid profiler contracts (`C1–C4`)**:
   - **Custom-op ABI, symbol shadowing & counting domain (`C1`)**: When hitting `ACLNN_ERR_PARAM_NULLPTR (161001)` or `561000`/`561103`, check whether `opapi_math` shadows `libopapi.so` or `_C_ascend`/`libascend_kernels.so` has CANN ABI drift (`ldd`/`nm -D`), and ensure `TORCH_LIBRARY_IMPL` registers both `PrivateUse1` and `Meta` kernels. For `npu_fused_infer_attention_score_v2` (`FIA`) `errcode 264` (`tiling offset out of range`) in MTP (`step >= 1`), align `actual_seq_lengths_kv` counting domain with tensor rank (`3D BSH/BSND` cumulative token count vs. `4D BNSD` sequence count). Pad non-32B-aligned `head_dim` (e.g., `35 -> 48`) before kernel launch and slice back to `[:35]`. Purge `.torchair_cache` and `~/.cache/vllm/torch_compile_cache` (or set `VLLM_DISABLE_COMPILE_CACHE=1`) after C++ custom-op or graph-pass edits, and never combine `--enforce-eager` with `cudagraph_mode`.
   - **Disk `D`-state vs. NPU hang, SHM/ZMQ liveness & thread NPU context (`C2`)**: When multi-card launch shows `0%` AICore utilization, check `/proc/<pid>/status` (`State`) and `/proc/<pid>/wchan` (`disk_io_wait`) first—`D` state in `lock_page_killable`/`nfs_*`/`folio_wait*` is cold storage page-cache I/O, not an HCCL deadlock. If `/v1/models` returns HTTP 200 while `/v1/chat/completions` hangs, inspect `EngineCore` `shm_broadcast`/`MessageQueue` ring buffers and ZMQ multipart framing. If `H2D/D2D` fails with `107002`/`507001`, check earlier logs for `camem` swallowed `207001` OOMs. Explicitly call `torch.npu.set_device(device_id)` in every `ThreadPoolExecutor`/ZMQ callback thread (`107003`), and trace rogue `device 0` allocations non-intrusively via `perf probe -x libascendcl.so 'aclrtSetDevice deviceId=%x0:s32'`.
   - **Zero-NPU Safetensors header preflight & GDN/KDA/DCP numerical invariants (`C3`)**: Inspect `.safetensors` 8-byte headers (`inspect_safetensors_checkpoint`) before acquiring NPU leases to catch missing/truncated shards and unquantized `BF16` routed experts inside W8A8 checkpoints. In hybrid linear attention (`GDN`/`KDA`), avoid double $1/\sqrt{d}$ query scaling across chunked prefill and index final states by `actual_seq_lengths_q` without overwriting persistent recurrent caches during speculative draft steps. In Ring/Ulysses/DCP attention, merge cross-rank LSE strictly in `float32` natural-log domain (`lse = m + log(l)`), never mixing base-2 `max` with base-$e$ `sum`.
   - **Hybrid Dense + MoE + MTP profiler segmentation (`C4`)**: Select the primary compute `streamId` by `argmax_s sum(duration_ns)` rather than `first_event`, keep `MlaPrologV3` and `KvQuantSparseFlashAttention` inside one MLA layer, and group `Dense prefix + MoE trunk + MTP draft` layers into a single decode step rather than splitting on operator-type transitions.

Design provenance: `mindie-agent/docs/knowledge-context-and-exploration.md`,
section 15, mechanisms M1-M6, workflows W1-W6, contracts C1-C4 and transcript
sources T1-T7. Those historical cases motivate the mechanisms; their hardware
parameters and specific fixes are not defaults for a new task.

# mindie-agent-codex 0.1.21

`mindie-agent-codex 0.1.21` refines the `mindie-agent` Skill and domain-skills workflow so RSI stagnation automatically promotes `requested_mode="reuse" -> effective_mode="explore"` (`auto_switched_from_reuse=True`) after 3 distinct refuted candidate repair hashes (`candidate_sha256`) in `logs/experiment-ledger.jsonl` without manual `mode="explore"` calls, and enforces strict workspace boundary discipline during evaluation.

| Component | Version | Source commit |
| --- | --- | --- |
| knowledge | 0.13.13 | `da64fe111d878f44b7efec6af6ca46d3f5887be6` |
| remote-dev | 0.11.5 | `131b24533479e9cefc572144adccdcb345f70846` |
| diagnostics | 0.5.6 | `9b331956b00737dd6d8c956da3416316f2de1ddf` |
| debugger | 0.1.0 | `e9406b2b178335190d5cb59dff4d6985894497ba` |
| profiler | 0.5.6 | `79d97e75bff7351b0472b93c03b072c1cf47f5e7` |
| sim | 0.9.3 | `dbadff04b60937a0722b0a227cdad01beed083cd` |
| coordinator | 0.7.4 | `14d7cec7247bdcbea9b9878db4770e3a05a6ea41` |
| npu-top | 0.3.1 | `8f0cb3ec66393812e90e118cd73704070c6027a2` |
| mindie-bench | 0.4.7 | `HEAD` |

## What's New (Human-Readable Summary)

- **Automatic `reuse -> explore` Promotion Workflow (`SKILL.md` / `references/domain-skills.md`)**: Guides the agent to record each distinct candidate repair (`candidate_sha256`) and test outcome in `logs/experiment-ledger.jsonl` and re-query with `mode="reuse"` (`caller_initiated_explore=False`), letting `mindie-knowledge` automatically promote to `effective_mode="explore"` after 3 distinct refuted candidate hashes.
- **Workspace Boundary & Anti-Oracle-Reverse-Engineering Discipline (`SKILL.md`)**: Explicitly forbids leaving the current workspace directory or brute-forcing SHA-256 test digests during `L3` candidate evaluation.
- **16-Trial End-to-End RSI A/B Benchmark Verification (`track-RSI-E2E-v4`)**: Verified on Codex (`gpt-6.1-sol xhigh`) across 4 Ascend 950DT (A5) controlled contract tasks: Control `G2` `0/4 (0.0%)` vs Treatment `G4` `4/4 (100.0%)`, with `4/4` automatic `reuse -> explore` promotions (`caller_initiated_explore=False`) and `4/4` `graded-c6` local Git commits (`completed:applied`), independently audited by Codex `gpt-6-astra (max)`.

# mindie-agent-codex 0.1.18

`mindie-agent-codex 0.1.18` unifies the Recursive Self-Improvement (`rsi_loop` / `experiment_ledger`) state machine onto `mindie-knowledge 0.13.11`, raises default graded knowledge retrieval token budgets (`explore` `24000`, `query` `8000`, `explain` `16000`, ceiling `64000`), fixes stale replaced service owner retirement during `service replace` (`#85`), and aligns all runtime and simulation component pins (`mindie-sim 0.9.3`).

| Component | Version | Source commit |
| --- | --- | --- |
| knowledge | 0.13.11 | `1392a5c326605225fc6d89334842ae7700c0dce8` |
| remote-dev | 0.11.4 | `7c67876d716feb3949e4dbf33eb5e06980ff0d36` |
| diagnostics | 0.5.5 | `f2f2f946c172afc99342a84c0c1ed244f6d44e01` |
| debugger | 0.1.0 | `e9406b2b178335190d5cb59dff4d6985894497ba` |
| profiler | 0.5.6 | `79d97e75bff7351b0472b93c03b072c1cf47f5e7` |
| sim | 0.9.3 | `dbadff04b60937a0722b0a227cdad01beed083cd` |
| coordinator | 0.7.3 | `79a8b28eef72d57f965a01c6660849f024fbb1c0` |
| npu-top | 0.3.1 | `8f0cb3ec66393812e90e118cd73704070c6027a2` |
| mindie-bench | 0.4.6 | `c7d0c8a4969b3816925c0405931ae17e28ce7a34` |

## What's New (Human-Readable Summary)

- **Single-Source RSI Loop & Experiment Ledger (`mindie-knowledge 0.13.11`)**: Consolidates RSI loop state onto `ExperimentLedger` (`orient` / `propose` / `verdict`), eliminates dual-state drift across `superseded` baselines and historical retries, and surfaces `active_rsi_session` directly in `knowledge_explore`.
- **Higher Graded Retrieval Token Budgets**: Raises default `token_budget` to `24000` for `knowledge_explore`, `8000` for `knowledge_query`, and `16000` for `knowledge_explain` (ceiling `64000`), avoiding premature truncation on complex multi-domain engineering tasks.
- **Stale Replaced Service Retirement (`#85`)**: Safely retires superseded `state=replaced` service owners and launch records during `service replace` and aligns `mindie-sim` to `0.9.3` (`dbadff04b60937a0722b0a227cdad01beed083cd`).

# Third-Party Notices

## InferRSI

The RSI experiment ledger pinned and dispatched by this plugin through the shared
`mindie-harness` substrate (`mindie_harness.rsi.experiment_ledger`, formerly bundled
in this repository) adapts two functions from
[InferRSI](https://github.com/InferMatrixLab/InferRSI) (Copyright InferRSI
authors), licensed under the Apache License 2.0:

- `fingerprint` is adapted from `src/inferrsi/infra/targets/git.py:repository_identity`.
- The evidence-completeness order in `check_trials` follows `src/inferrsi/harness/evaluation.py:evaluate`.

Both were rewritten in pure Python stdlib; the 4-stage RSI candidate verification
gates are evaluated directly in `mindie_harness.rsi.experiment_ledger`. The full
license text is retained in [`licenses/InferRSI-Apache-2.0.txt`](licenses/InferRSI-Apache-2.0.txt).

## vllm-evolve

The duplicate-source and run/card-time budget check in `experiment_ledger gate`
(`mindie_harness.rsi.experiment_ledger`) follows the mechanism of
[vllm-evolve](https://github.com/JiusiServe/vllm-evolve) `bench/eval_pool.py`
(`HashCache`, `EvalBudget`). No code was copied.

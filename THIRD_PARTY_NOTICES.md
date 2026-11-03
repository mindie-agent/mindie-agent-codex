# Third-Party Notices

## InferRSI

`plugins/mindie-agent/scripts/experiment_ledger.py` adapts two functions from
[InferRSI](https://github.com/InferMatrixLab/InferRSI) (Copyright InferRSI
authors), licensed under the Apache License 2.0:

- `fingerprint` is adapted from `src/inferrsi/infra/targets/git.py:repository_identity`.
- The evidence-completeness order in `judge` follows `src/inferrsi/harness/evaluation.py:evaluate`.

Both were rewritten for this plugin (stdlib only, judgment delegated to the
MindIE knowledge engine). The full license text is in
[`licenses/InferRSI-Apache-2.0.txt`](licenses/InferRSI-Apache-2.0.txt).

## vllm-evolve

The duplicate-source and run/card-time budget check in `experiment_ledger.py gate`
follows the mechanism of [vllm-evolve](https://github.com/JiusiServe/vllm-evolve)
`bench/eval_pool.py` (`HashCache`, `EvalBudget`). No code was copied.

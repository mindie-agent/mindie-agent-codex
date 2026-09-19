# Domain skills

Read a domain skill on demand after activation, only when the task matches its
selector:

- `vllm-ascend-serving` — start, inspect or stop one single-node service
- `vllm-ascend-pd-serving` — prefill/decode topology deployments
- `vllm-ascend-benchmark` — throughput and latency measurement
- `vllm-ascend-performance-regression` — controlled baseline-vs-candidate experiments
- `vllm-ascend-correctness-validation` — output/accuracy comparison across code states
- `vllm-ascend-change-validation` — consolidate existing evidence into a validation report
- `vllm-ascend-distributed-debug` — multi-rank and multi-node failures
- `vllm-ascend-graph-debug` — cudagraph/ACL Graph failures, graph-vs-eager divergence
- `ascend-operator-debug` — reduce a failure to one operator
- `ascend-tensor-dump` — localize numerical divergence across stages
- `ascend-memory-profiling` — HBM attribution
- `ascend-profiling-collection` — torch-profiler case capture
- `ascend-profiling-analysis` — analysis of already collected profiling data
- `modelscope` — ModelScope weight download, resume, status, verification

Their CLI tools bind the user's current business directory and take explicit
remote targets; managed execution goes through the configured coordinator.
Poll owned jobs and retrieve their artifacts through remote-dev; reuse the
returned full container ID.

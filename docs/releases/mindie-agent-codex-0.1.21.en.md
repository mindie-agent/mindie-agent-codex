# mindie-agent-codex 0.1.21

[中文](mindie-agent-codex-0.1.21.md)

## Task continuity and first-time SSH access

- Reuse supplied credentials through shared `remote_auth` to authenticate, install a public key, and verify key access. Missing saved authentication profiles must not block otherwise authorized work.
- Optional knowledge, diagnostics, component setup, and local accounting failures affect their capability. Continue independent authorized native work while preserving task identity, resource constraints, and active jobs.
- Preserve successful primary remote outcomes with explicit warnings for local receipt failures. Reconcile uncertain external writes before retrying them.
- Validation covers local contracts, platform CI, real isolated SSH servers, and release benchmarks. No private deployment-host, NPU inference, or model accuracy acceptance is claimed.

| Component | Version | Source commit |
| --- | --- | --- |
| knowledge | 0.13.13 | `3c478a19b5542b38520c7f98b87a07f37e305021` |
| remote-dev | 0.11.6 | `991ef7863b2b208f94f54a29db4ac5be50884ea3` |
| diagnostics | 0.5.7 | `f338788cecc56f7aff1dccab1d53426c9e28fe7f` |
| debugger | 0.1.0 | `e9406b2b178335190d5cb59dff4d6985894497ba` |
| profiler | 0.5.6 | `79d97e75bff7351b0472b93c03b072c1cf47f5e7` |
| sim | 0.9.4 | `6f1d5a871a7b1ef20ff7a8cab33aaf4637fb3f66` |
| coordinator | 0.7.5 | `819b0deb7bea994d4dff9c009200a48dd9300cc4` |

The Skill also guides RSI stagnation through three distinct refuted repair hashes before automatic reuse-to-explore promotion, with workspace boundaries preserved. The previously recorded controlled CPU RSI experiments remain historical evidence in the [product 0.1.8 report](https://github.com/mindie-agent/mindie-agent/blob/main/docs/releases/0.1.8/rsi-e2e-auto-switch-ab-report.md); they are not rerun or relabeled as validation of this patch.

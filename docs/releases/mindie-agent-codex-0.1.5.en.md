# mindie-agent-codex 0.1.5

[中文](mindie-agent-codex-0.1.5.md) · **English**

Fix automatic plugin upgrades after independent component updates. Bootstrap launches `component_update.py` with `python -I`, which omits the script directory from the module search path. The missing path initialization caused `ModuleNotFoundError: No module named 'bounded_process'` after native installation but before runtime activation. The component entry now selects its own script directory and retains isolation; bootstrap also passes `-B`.

Candidate validation now starts the real isolated component CLI through its model-free, network-free `--help` path. Regression tests cover Unicode and space-containing paths, ambient `PYTHONPATH` interference, and a missing candidate dependency. Existing update contracts cover preserving the active binding on failure, busy-service deferral, cancellation, concurrent checks, and native Hook trust inheritance.

Runtime pins remain the formal releases `knowledge 0.13.4`, `remote-dev 0.10.6`, and `diagnostics 0.5.2`. The Tag workflow delivers full component tests and release benchmarks. Native automatic-update acceptance on other devices remains separate.

Devices already stuck on an older pending candidate may need one recovery because their old updater resumes that candidate before selecting another release. A newly published Release cannot replace code that has not reached installation. The plugin directory version alone does not prove runtime activation.

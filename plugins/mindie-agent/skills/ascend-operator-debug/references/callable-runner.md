# Explicit callable cases

Use the runner for an ordinary Python wrapper with the same argument signature
as its reference. Supply three actual Python entry files, not result forms:

```python
# kernel.py — replace this example with the actual operator/Triton wrapper.
def run(x):
    return x + x
```

```python
# reference.py
def run(x):
    return x * 2
```

```python
# cases.py
import torch

def cases(device):
    torch.manual_seed(7)
    yield {"id": "contiguous", "args": (torch.randn(16, 128, device=device),)}
    yield {"id": "strided", "args": (torch.randn(128, 16, device=device).t(),),
           "atol": 1e-5, "rtol": 1e-5}
```

The `run` subcommand accepts `--kernel kernel.py:run`,
`--reference reference.py:run` and `--cases cases.py:cases`. Each case has a
unique `id`, positional `args`, optional `kwargs`, and optional `atol`/`rtol`.
Global tolerances default to 1e-5 and can be changed explicitly. NPU cases need
an actual NPU input tensor. `--device cpu` deliberately selects CPU execution
with no NPU allocation; it does not count as Ascend acceptance.

The files are read once, hashed and embedded in coordinator's `script_file`.
They are unpacked inside that execution's owned cwd, with no original local
absolute paths or donor source paths inserted. Imports may use the other two
entry files or the task's selected project sources. Extra local helper/data
files are not silently copied: place them in an explicitly selected source or
use the normal managed script entry for a larger reproduction. Different files
with the same basename are rejected. No import of business code occurs locally.
When entry points share one file, that file is loaded once. Separate entry
files should be self-contained wrappers; do not rely on shared module-global
state across them. This is not a general project packaging or test runner.

The runner deep-copies the combined args/kwargs before each call, preserving
shared references within that object tree. Ordinary leaf tensor strides are
retained. Non-leaf tensors and custom objects that cannot be deep-copied fail
visibly; adapt the business factory when needed. In-place wrappers return the
mutated values that should be compared. Supported outputs are tensors, numbers,
booleans/strings and nested tuple/list/dict structures; `None` and structures
with no output values are not evidence (an empty tensor is still a tensor).
Compilation, graph capture/replay and custom launch conventions stay in the
explicit wrapper, rather than being guessed by the runner.

Native context is resolved by the official task client; `--context-file` reuses
an explicit existing association. Sources default to the native selection;
`--source NAME=PATH` overrides it, and `--no-project-sources` deliberately uses
none. `--host` and `--image` are ordinary coordinator constraints. No container
selection, provisioning sequence or separate allocation is added.

The default wait is 180 seconds. `--wait-timeout-seconds` accepts 0–600 and
`--timeout-seconds` controls the actual execution limit (default 600). A pending
receipt retains its execution ID; continue that execution through the ordinary
owner wait/tail tools. Never submit again merely because the wait expired.

Stdout summarizes the observed callable invocation, errors, comparisons and
optional paired timings. `record_ref` points to the complete local owner reply;
`business.record_path` is the detailed result in the remote owned directory,
including actual input shapes/strides, source hashes, tracebacks and timing
samples. The code remains there after failure. These facts prove invocation of
the named wrapper, not which internal device kernels it chose. Inspect existing
source or profiler evidence when that distinction matters. Imported legacy
result files never become fresh launch evidence.

# Report input example

This is an illustrative configuration shape. Select cases and values for the
requested report. `required_stages` names the coverage to summarize; it does
not schedule work or impose stages on development. The tool generates report
metadata internally; observed output files come from the existing execution
or measurement harness.

```json
{
  "op_name": "softmax",
  "source": {
    "kind": "gpu-triton",
    "path": "/src/softmax.py"
  },
  "target": {
    "soc": "Ascend910B2"
  },
  "required_stages": [
    "development",
    "validation"
  ]
}
```

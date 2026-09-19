# Existing-evidence report input example

This is the optional report configuration for supplied observations. For actual
execution inputs, use [callable inputs](../../ascend-operator-debug/references/callable-runner.md).
The report generates its own metadata; reuse results from the relevant execution
or measurement harness.

```json
{
  "op_name": "softmax",
  "reference": {
    "path": "/src/ref.py"
  },
  "target": {
    "soc": "Ascend910B2"
  },
  "tolerances": {
    "float16": {
      "atol": 0.001,
      "rtol": 0.001
    }
  },
  "cases": [
    {
      "id": "case-1",
      "mode": "eager",
      "inputs": [
        {
          "name": "x",
          "shape": [
            2,
            4
          ],
          "strides": [
            4,
            1
          ],
          "dtype": "float16",
          "layout": "ND"
        }
      ]
    }
  ]
}
```

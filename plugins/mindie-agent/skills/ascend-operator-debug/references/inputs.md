# Existing-evidence report input example

This is the optional report configuration for supplied observations. For actual
execution inputs, use [callable inputs](callable-runner.md). The report generates
its own metadata; reuse results from the relevant execution or measurement harness.

```json
{
  "operator": {
    "name": "npu_example",
    "invocation": "torch_npu.npu_example(x)",
    "reference": "torch_ref(x.cpu())"
  },
  "tolerance": {
    "atol": 0.001,
    "rtol": 0.001
  },
  "source_model_failure": "model output diverges",
  "cases": [
    {
      "id": "fp16-eager",
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
      ],
      "attributes": {
        "transpose": false
      }
    },
    {
      "id": "fp16-graph",
      "mode": "graph",
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
      ],
      "attributes": {
        "transpose": false
      }
    }
  ]
}
```

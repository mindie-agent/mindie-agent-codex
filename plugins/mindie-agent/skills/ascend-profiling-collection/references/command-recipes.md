# Command recipes

```bash
python /absolute/plugin/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py \
  --model /data/models/Qwen --served-model-name Qwen --tp 2 \
  --tag smoke --mode enforce_eager --request-kind text \
  --benchmark-output-tokens 32

python /absolute/plugin/skills/ascend-profiling-collection/scripts/profile_control.py \
  --service vllm --action start_profile

python /absolute/plugin/skills/ascend-profiling-collection/scripts/run_remote_analyse.py \
  --execution-id <id> --profile-root /srv/profiling/capture
```

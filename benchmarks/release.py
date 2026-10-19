"""[Role] Measure a repeatable public-input component workload for releases.
[Invariants] Assert business output before reporting latency; no productivity claim.
[Timeouts & Locks] Bounded iterations, no network or background services.
[Side-Effect Policy] Temporary synthetic files and an explicit result JSON only.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins/mindie-agent/scripts"))
import codex_transcript
records = [{"type": "session_meta", "payload": {"id": "bench-session", "cwd": "/work"}}]
for i in range(100):
    records.append({"timestamp": "2026-10-19T00:00:00Z", "type": "response_item",
                    "payload": {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "Public NPU mapping observation " + str(i)}]}})
records.append({"type": "response_item", "payload": {"type": "reasoning", "text": "HIDDEN_SENTINEL"}})
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "rollout.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    iterations = 100
    started = time.perf_counter()
    for _ in range(iterations):
        result = codex_transcript.read_increment(str(path), 0, session_id="bench-session")
    elapsed = time.perf_counter() - started
    assert result["status"] == "ok" and "observation 99" in result["text"]
    assert "HIDDEN_SENTINEL" not in result["text"]
METRIC = "public_100_message_parse_latency"

Path(os.environ["MINDIE_BENCH_RESULT_PATH"]).write_text(json.dumps({
    "status": "pass", "metrics": [{"name": METRIC, "unit": "us/op",
    "value": elapsed * 1_000_000 / iterations, "iterations": iterations}]
}) + "\n", encoding="utf-8")

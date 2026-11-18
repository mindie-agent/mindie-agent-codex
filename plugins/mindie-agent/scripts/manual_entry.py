"""[Role] Passive UserPromptSubmit hook handler and event reader.

[Invariants]
- All prompts pass through passively without requiring prompt prefixes or gestures.
- Cold prompts never read configuration or initialize SQLite state.

[Timeouts & Locks]
- Input is bounded at 32 MiB with zero file locks or external I/O.

[Side-Effect Policy]
- Passive UserPromptSubmit mutates no Admission lease and starts no service,
  corpus, model, transcript or contribution worker.
"""
import json


def requested(event):
    """UserPromptSubmit is always a fast passive pass-through."""
    return False


def read_event(stream):
    raw = stream.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("native prompt hook exceeds its byte budget")
    text = raw.decode("utf-8-sig") if isinstance(raw, (bytes, bytearray)) else str(raw).lstrip("\ufeff")
    return json.loads(text)


def action(event):
    return ""


def authorize(adapter, event):
    return {}

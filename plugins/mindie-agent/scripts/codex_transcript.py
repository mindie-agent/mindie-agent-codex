"""Bounded incremental reader for native Codex JSONL task transcripts.
[Role] Export `FileIdentity`, `identify`, `same_file`, `history_source`, `capture_source`, `read_material`, and `read_increment` for Codex rollout JSONL.
[Invariants] Extract only public user/assistant messages; exclude tools, hidden reasoning, injected instructions, and foreign tasks; enforce exact `[start, end)` SHA-256 accounting.
[Timeouts & Locks] `read_material` bounds scans to 16 MiB / 2.0s without locks.
[Side-Effect Policy] Purely read-only over the caller-specified transcript path.
"""
from __future__ import annotations
import hashlib, json, os, re, time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
MAX_WINDOW, MAX_TEXT, MAX_RECORDS, ANCHOR_BYTES = 256 * 1024, 4 * 1024 * 1024, 200, 512
PUBLIC, KNOWN = {None, "final", "final_answer", "commentary"}, {"session_meta", "turn_context", "response_item", "event_msg", "compacted"}
INJECTED_PREFIXES = ("<recommended_plugins>", "<environment_context>", "# AGENTS.md instructions", "<permissions instructions>", "<skills_instructions>", "<skill>", "<app-context>")
ATTACHMENTS = {"input_image": "Image", "image": "Image", "input_audio": "Audio", "input_file": "File"}
class TranscriptSourceError(ValueError):
    def __init__(self, code: str, message: str) -> None: super().__init__(message); self.code = code
def _resolved(path, *, strict=False):
    from mindie_knowledge.filesystem import resolved_path
    return resolved_path(path, strict=strict)
def _identity(path):
    from mindie_knowledge.filesystem import path_identity
    return path_identity(path)
@dataclass(frozen=True)
class FileIdentity:
    path: str; dev: int; ino: int; size: int; mtime_ns: int; anchor_len: int = 0; anchor_digest: str = ""
    def anchor_for(self, count):
        try: fd = os.open(_resolved(self.path), os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except TimeoutError: raise
        except (OSError, ValueError): return None
        try: return hashlib.sha256(os.read(fd, count)).hexdigest()
        except TimeoutError: raise
        except OSError: return None
        finally: os.close(fd)
    def serialize(self) -> str: return json.dumps(dict(dev=self.dev, ino=self.ino, anchor_len=self.anchor_len, anchor_digest=self.anchor_digest), sort_keys=True, separators=(",", ":"))
    @staticmethod
    def unserialize(text, path):
        try:
            d = json.loads(text); n, dg = d["anchor_len"], d["anchor_digest"]
            return FileIdentity(path, int(d["dev"]), int(d["ino"]), 0, 0, n, dg) if type(n) is int and 0 < n <= ANCHOR_BYTES and isinstance(dg, str) and len(dg) == 64 else None
        except (ValueError, KeyError, TypeError): return None
def identify(path) -> FileIdentity | None:
    try: fd = os.open(_resolved(path), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except TimeoutError: raise
    except (OSError, ValueError): return None
    try: st, anchor = os.fstat(fd), os.read(fd, ANCHOR_BYTES)
    except TimeoutError: raise
    except OSError: return None
    finally: os.close(fd)
    return FileIdentity(str(_resolved(path)), st.st_dev, getattr(st, "st_ino", 0), st.st_size, st.st_mtime_ns, len(anchor), hashlib.sha256(anchor).hexdigest())
def same_file(identity: FileIdentity | None, current: FileIdentity | None) -> bool:
    if identity is None or current is None or _identity(identity.path) != _identity(current.path): return False
    if (identity.dev and current.dev and identity.dev != current.dev) or (identity.ino and current.ino and identity.ino != current.ino): return False
    return bool(identity.anchor_len and identity.anchor_digest) and current.anchor_for(identity.anchor_len) == identity.anchor_digest
def _timestamp(record):
    if not isinstance(raw := record.get("timestamp"), str): return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).timestamp()
    except ValueError: return None
def _extract(record):
    if not isinstance(payload := record.get("payload"), dict): return None
    if record.get("type") == "session_meta": return ("meta", None)
    if record.get("type") != "response_item" or payload.get("channel") not in PUBLIC or payload.get("phase") not in PUBLIC: return None
    if (ptype := payload.get("type")) not in {"message", "agent_message"}: return None
    if (role := payload.get("role", "assistant" if ptype == "agent_message" else None)) not in {"user", "assistant"}: return None
    if ptype == "agent_message" and isinstance(payload.get("text"), str): parts = [payload["text"]]
    elif isinstance(payload.get("content"), list):
        parts = [item["text"] if item.get("type") in {"input_text", "output_text"} else f"[{ATTACHMENTS[item['type']]} attachment omitted]" for item in payload["content"] if isinstance(item, dict) and item.get("channel") in PUBLIC and ((item.get("type") in {"input_text", "output_text"} and isinstance(item.get("text"), str)) or item.get("type") in ATTACHMENTS)]
    else: return None
    if role == "user": parts = [p for p in parts if not p.lstrip().startswith(INJECTED_PREFIXES)]
    text, phase = re.sub(r"<oai-mem-citation>.*?</oai-mem-citation>", "", "\n".join(parts), flags=re.S), (payload.get("phase") or payload.get("channel"))
    return (role + (":" + phase if role == "assistant" and phase else ""), text) if text.strip() else None
def _session_of(record):
    return record["payload"].get("id") if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict) and isinstance(record["payload"].get("id"), str) else None
def history_source(path):
    path = _resolved(path)
    if not path.is_file(): raise ValueError("history source must be a regular transcript file")
    with path.open("rb") as s: info, anchor = os.fstat(s.fileno()), s.read(ANCHOR_BYTES); s.seek(0); raw = s.readline(info.st_size)
    try: meta = json.loads(raw); owner, scope = _session_of(meta), meta["payload"]["cwd"]
    except (ValueError, TypeError, KeyError): raise ValueError("history source requires native session metadata and project scope") from None
    if not raw.endswith(b"\n") or not owner or not isinstance(scope, str) or not Path(scope).is_absolute():
        raise ValueError("history source requires native session metadata and project scope")
    return dict(session_id=owner, project_root=str(_resolved(scope)), identity=FileIdentity(str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, len(anchor), hashlib.sha256(anchor).hexdigest()))
def capture_source(path, session_id, *, codex_home=None):
    try: profile, selected = _resolved(codex_home if codex_home is not None else (os.environ.get("CODEX_HOME") or Path.home() / ".codex")), _resolved(path, strict=True)
    except TimeoutError: raise
    except (OSError, ValueError) as exc: raise TranscriptSourceError("transcript_unavailable", "native transcript path is unavailable") from exc
    if not any(Path(_identity(selected)).is_relative_to(Path(_identity(profile)) / name) for name in ("sessions", "archived_sessions")):
        raise TranscriptSourceError("transcript_outside_profile", "capture transcript is outside the active native profile")
    try:
        with selected.open("rb") as s: header = s.readline(1024 * 1024 + 1)
    except TimeoutError: raise
    except OSError as exc: raise TranscriptSourceError("transcript_unavailable", "native transcript read is unavailable") from exc
    if len(header) > 1024 * 1024 or not header.endswith(b"\n"): raise TranscriptSourceError("transcript_header_invalid", "native task header is missing or too large")
    try: meta = json.loads(header)
    except ValueError as exc: raise TranscriptSourceError("transcript_header_invalid", "native task header is invalid") from exc
    if not isinstance(meta, dict) or _session_of(meta) != session_id: raise TranscriptSourceError("transcript_identity_mismatch", "capture transcript belongs to another task")
    payload, scope = meta["payload"], meta["payload"].get("cwd"); created = _timestamp({"timestamp": payload.get("timestamp")}) or _timestamp(meta)
    if not isinstance(scope, str) or not Path(scope).is_absolute() or created is None:
        raise TranscriptSourceError("transcript_metadata_invalid", "native task scope or creation boundary is unavailable")
    return dict(session_id=session_id, project_root=str(_resolved(scope)), created_at=created, transcript_path=str(selected))
def read_material(path, start, *, session_id=None, not_before=None, expected=None, max_scan_bytes=16777216, max_seconds=2.0, max_text_bytes=MAX_TEXT, scan_until=None):
    if type(start) is not int or start < 0 or max_scan_bytes <= 0 or max_seconds <= 0 or max_text_bytes <= 0: raise ValueError("invalid read budget")
    if scan_until is not None and (type(scan_until) is not int or scan_until < start): raise ValueError("scan_until must be an exact byte boundary at or after start")
    result = dict(status="ok", start=start, end=start, digest=hashlib.sha256(b"").hexdigest(), text="", records=0, skipped_records=0, partial=False, more=False, session_match=None, coverage_note=None)
    consumed, included, size, begun, recognized = hashlib.sha256(), [], 0, time.monotonic(), 0
    try:
        with open(_resolved(path), "rb") as stream:
            stat, anchor = os.fstat(stream.fileno()), stream.read(ANCHOR_BYTES)
            current = FileIdentity(str(_resolved(path)), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, len(anchor), hashlib.sha256(anchor).hexdigest())
            result["identity"], result["snapshot_size"] = current.serialize(), stat.st_size
            if expected is not None and not same_file(expected, current): return dict(result, status="replaced", coverage_note="transcript changed before read")
            if stat.st_size < start: return dict(result, status="replaced", coverage_note="transcript truncated")
            stream.seek(0); header = stream.readline(1024 * 1024 + 1)
            if len(header) > 1024 * 1024: return dict(result, status="unknown-format", coverage_note="transcript header exceeds bound")
            try: meta = json.loads(header)
            except ValueError: meta = None
            owner = _session_of(meta) if isinstance(meta, dict) else None
            if session_id and owner != session_id: return dict(result, status="wrong-task" if owner else "unknown-format", coverage_note="transcript owner unavailable")
            result["session_match"], parent = (True if owner else None), (meta.get("payload", {}).get("forked_from_id") if isinstance(meta, dict) else None)
            fork_time = _timestamp({"timestamp": meta["payload"].get("timestamp")}) if parent else None
            if parent and fork_time is None: return dict(result, status="unknown-format", coverage_note="fork timestamp unavailable")
            effective_end = min(stat.st_size, scan_until) if scan_until is not None else stat.st_size
            if start > 0 and (stream.seek(start - 1) or True) and stream.read(1) != b"\n":
                return dict(result, status="replaced", coverage_note="cursor is not a whole-record boundary")
            stream.seek(start); limit = min(effective_end, start + max_scan_bytes)
            while stream.tell() < limit and time.monotonic() - begun < max_seconds:
                offset = stream.tell(); raw = stream.readline(min(limit - offset, MAX_TEXT + 65536))
                if not raw: break
                if not raw.endswith(b"\n"): result["partial"] = offset + len(raw) == stat.st_size; break
                try: record = json.loads(raw)
                except (ValueError, UnicodeDecodeError): record = None
                if not isinstance(record, dict): return dict(result, status="invalid-record", coverage_note="invalid complete record")
                if record.get("type") in KNOWN: recognized += 1
                if (foreign := _session_of(record)) and session_id and foreign != session_id and foreign != parent:
                    return dict(result, status="wrong-task", session_match=False)
                extracted = _extract(record)
                if fork_time is not None and ((stamp := _timestamp(record)) is None or stamp < fork_time): extracted = None
                if extracted and extracted[1]:
                    stamp = _timestamp(record)
                    if not_before is not None and stamp is None: return dict(result, status="invalid-record", end=start, text="", coverage_note="missing public timestamp")
                    if not_before is None or stamp >= not_before:
                        rendered = f"### {extracted[0]}\n" + extracted[1].replace("\r\n", "\n").replace("\r", "\n"); added = len(rendered.encode()) + 2
                        if added > max_text_bytes:
                            if included: break
                            result["skipped_records"] += 1
                        elif size + added > max_text_bytes or len(included) >= MAX_RECORDS: break
                        else: included.append(rendered); size += added
                    else: result["skipped_records"] += 1
                else: result["skipped_records"] += 1
                consumed.update(raw); result["end"] = stream.tell()
            result["more"] = result["end"] < effective_end
    except TimeoutError: raise
    except OSError: return dict(result, status="missing", coverage_note="transcript unreadable")
    result.update(digest=consumed.hexdigest(), text="\n\n".join(included), records=len(included))
    if result["end"] == start: result["status"] = "unchanged"
    elif result["status"] == "ok" and not recognized: result.update(status="unknown-format", text="", coverage_note="no recognized native signature")
    return result
def read_increment(path, start, *, session_id=None, not_before=None, expected=None):
    return read_material(path, start, session_id=session_id, not_before=not_before, expected=expected, max_scan_bytes=MAX_WINDOW)

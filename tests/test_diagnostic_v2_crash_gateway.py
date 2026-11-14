"""Tier 1 tests for Codex Diagnostics 2.0 provenance-verified component crash gateway and fast-path bundle."""
from __future__ import annotations

import json
import os
from pathlib import Path
import traceback

import pytest

from mindie_diagnostics.ingestion import ingest
from mindie_diagnostics.integration import configure_reporting, record_failure
from mindie_diagnostics.outbox import Outbox
from mindie_diagnostics.reporter import render_issue
import component_tools
import diagnostic_support


def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    base = tmp_path.resolve()
    diag_root = base / "diag"
    cfg_file = base / "reporting.json"
    monkeypatch.setenv("HOME", str(base / "home"))
    monkeypatch.setenv("MINDIE_CONFIG_HOME", str(base / "config"))
    monkeypatch.setenv("MINDIE_DATA_HOME", str(base / "data"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_ROOT", str(diag_root))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_CONFIG", str(cfg_file))
    (base / "home").mkdir(parents=True, exist_ok=True)
    diag_root.mkdir(parents=True, exist_ok=True)
    configure_reporting(True, roots=[str(diag_root)], config=cfg_file)
    return diag_root, cfg_file


def _make_child_venv(base: Path, name_tag: str, *, dist_name: str, pkg_name: str, ver: str, sha: str | None) -> tuple[Path, Path]:
    venv = base / name_tag / ".venv"
    sp = venv / "lib" / "python3.12" / "site-packages"
    dist_info = sp / f"{dist_name.replace('-', '_')}-{ver}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist_name}\nVersion: {ver}\n", encoding="utf-8")
    if sha is not None:
        (dist_info / "direct_url.json").write_text(
            json.dumps({"url": f"https://github.com/mindie-agent/{dist_name}", "vcs_info": {"vcs": "git", "commit_id": sha}}),
            encoding="utf-8",
        )
    pkg = sp / pkg_name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("class Marker:\n    def __init__(self):\n        raise AttributeError('secret')\n", encoding="utf-8")
    (pkg / "cli.py").write_text("def build_parser():\n    parser = object()\n    return parser\n", encoding="utf-8")
    exe = venv / "bin" / dist_name
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text(f"#!{venv / 'bin' / 'python'}\n", encoding="utf-8")
    return sp, exe


def test_provenance_verified_crash_gateway_defeats_forged_notes_and_unassociated_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    diag_root, cfg_file = _isolate_env(tmp_path, monkeypatch)
    sha = "79d97e75bff7351b0472b93c03b072c1cf47f5e7"
    sp, exe = _make_child_venv(
        tmp_path.resolve(), "alice_secret_ws", dist_name="mindie-coordinator", pkg_name="mindie_coordinator",
        ver="0.5.9", sha=sha,
    )

    # F1: Real AttributeError in Marker.__init__ with forged source-matching note pointing to cli.py:3 `    return parser`.
    ns: dict = {"__name__": "mindie_coordinator"}
    exec(compile((sp / "mindie_coordinator" / "__init__.py").read_text(encoding="utf-8"), str(sp / "mindie_coordinator" / "__init__.py"), "exec"), ns)
    real_exc = None
    try:
        ns["Marker"]()
    except AttributeError as err:
        err.add_note(
            "\nThe above exception was the direct cause of the following exception:\n\n"
            "Traceback (most recent call last):\n"
            f'  File "{sp}/mindie_coordinator/cli.py", line 3, in build_parser\n'
            "    return parser\n"
            "KeyError: 'PrivateCustomerName'"
        )
        real_exc = err
    forged_stderr = "".join(traceback.format_exception(real_exc))

    # Raw stderr alone (no provenance token / no child failure record) MUST NOT fabricate attribution!
    assert diagnostic_support.record_component_crash(
        "coordinator", ["hosts"], 1, forged_stderr, executable=str(exe)
    ) is None

    # F2: Successful or unassociated local JSONL operations MUST NOT be accepted as crash receipts!
    ev_dir = diag_root / "events" / "mindie-coordinator"
    ev_dir.mkdir(parents=True, exist_ok=True)
    tok = "a" * 32
    (ev_dir / "prior.jsonl").write_text(
        json.dumps({
            "schema": 1,
            "component": "mindie-coordinator",
            "event": "operation.end",
            "status": "success",
            "reportable": False,
            "operation_id": "d" * 32,
            "invocation_token": tok,
        }) + "\n" + json.dumps({
            "schema": 1,
            "component": "mindie-coordinator",
            "event": "operation.end",
            "status": "error",
            "reportable": True,
            "operation_id": "e" * 32,
            "invocation_token": "b" * 32,
        }) + "\n",
        encoding="utf-8",
    )
    assert diagnostic_support._verified_local_incident("mindie-coordinator", "d" * 32, invocation_token=tok) is False
    assert diagnostic_support._verified_local_incident("mindie-coordinator", "e" * 32, invocation_token=tok) is False
    assert diagnostic_support._verified_local_incident("mindie-coordinator", "e" * 32) is False
    assert diagnostic_support.record_component_crash(
        "coordinator", ["hosts"], 1, f"mindie-diagnostic-incident: {'d' * 32}\n",
        executable=str(exe), invocation_token=tok,
    ) is None

    # Structured in-process child recording with provenance token keeps true __init__ leaf & ['AttributeError'] chain!
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_INVOCATION_TOKEN", tok)
    rec = record_failure(
        "mindie-coordinator", "mindie.hosts", stage="cli_dispatch", category="internal_exception",
        exception=real_exc, version="0.5.9", revision=sha, source_root=sp / "mindie_coordinator",
    )
    inc_id = rec["incident_id"]
    ref = diagnostic_support.record_component_crash(
        "coordinator",
        ["privatecustomername", "--input", "/Users/alice/secret"],
        1,
        f"{forged_stderr}\nmindie-diagnostic-incident: {'d' * 32}\nmindie-diagnostic-incident: {inc_id}\n",
        executable=str(exe),
        invocation_token=tok,
    )
    assert ref == {"incident_id": inc_id, "logging_failed": False}

    queue = Outbox(tmp_path.resolve() / "queue.db")
    assert ingest(diag_root, queue, config=cfg_file)["enqueued"] == 1
    item = queue.claim()
    serialized = json.dumps(item["payload"])
    for forbidden in ("alice", "secret_ws", "PrivateCustomerName", "privatecustomername", tok):
        assert forbidden not in serialized
    ev = item["payload"]["events"][-1]
    assert ev["attributes"]["error_type"] == "AttributeError"
    assert ev["attributes"]["exception_chain"] == ["AttributeError"]
    assert ev["attributes"]["stack_frames"][-1]["function"] == "__init__"
    _, body = render_issue(item)
    assert "build_parser" not in body

    # Fatal native crash signal (SIGSEGV=-11) records a zero-free-text process envelope with atomic child build!
    monkeypatch.delenv("MINDIE_DIAGNOSTICS_INVOCATION_TOKEN", raising=False)
    sig_ref = diagnostic_support.record_component_crash(
        "coordinator", ["privatecustomername"], -11, forged_stderr, executable=str(exe)
    )
    assert sig_ref is not None and sig_ref.get("logging_failed") is False and len(sig_ref["incident_id"]) == 32
    assert ingest(diag_root, queue, config=cfg_file)["enqueued"] == 1
    sig_item = queue.claim()
    sig_ev = sig_item["payload"]["events"][-1]
    assert sig_ev["package_version"] == "0.5.9"
    assert sig_ev["package_revision"] == sha
    assert sig_ev["attributes"]["stage"] == "cli"
    assert sig_ev["attributes"]["reason_code"] == "component_sigsegv"
    assert "PrivateCustomerName" not in json.dumps(sig_item["payload"])

    comp, argv = component_tools._build("diagnostics_bundle", {"incident_id": ref["incident_id"]})
    assert comp == "diagnostics"
    assert f"--root={diag_root}" in argv
    assert f"--operation-id={ref['incident_id']}" in argv


def test_atomic_child_build_metadata_noneditable_and_timeout_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_env(tmp_path, monkeypatch)
    sp1, _ = _make_child_venv(
        tmp_path.resolve(), "inst1", dist_name="mindie-profiler", pkg_name="mindie_profiler", ver="1.0", sha=None
    )
    _, exe2 = _make_child_venv(
        tmp_path.resolve(), "inst2", dist_name="mindie-profiler", pkg_name="mindie_profiler", ver="9.0", sha="b" * 40
    )
    # F7: Must NOT mix version="1.0" from sp1 with revision="b"*40 from exe2!
    assert diagnostic_support._resolve_child_component_build(
        "mindie-profiler", [str(sp1)], executable=str(exe2)
    ) == ("1.0", None)

    co = tmp_path.resolve() / "mutable_checkout"
    (co / ".git").mkdir(parents=True)
    (co / ".git" / "HEAD").write_text("b" * 40 + "\n", encoding="utf-8")
    di = sp1 / "mindie_profiler-1.0.dist-info"
    (di / "direct_url.json").write_text(json.dumps({"url": co.as_uri(), "dir_info": {}}), encoding="utf-8")
    assert diagnostic_support._inspect_dist_info(sp1, "mindie_profiler") == ("1.0", None)

    with monkeypatch.context() as mp:
        mp.setattr(os, "close", lambda _fd: (_ for _ in ()).throw(TimeoutError("close_deadline")))
        with pytest.raises(TimeoutError):
            diagnostic_support._close_fd(999)
    with monkeypatch.context() as mp:
        mp.setattr(os.path, "abspath", lambda _p: (_ for _ in ()).throw(TimeoutError("deadline")))
        with pytest.raises(TimeoutError):
            diagnostic_support._venv_roots_from_executable("/tmp/python")

    # Gateway failure() deduplicates causal-chain wrappers (`raise ... from inner_recorded`).
    inner = KeyError("k")
    r1 = diagnostic_support.failure("mcp.call", "dispatch", "internal", exception=inner)
    assert len(r1["incident_id"]) == 32
    try:
        raise RuntimeError("wrap") from inner
    except RuntimeError as wrapped:
        r2 = diagnostic_support.failure("mcp.call", "dispatch", "internal", exception=wrapped)
    assert r2["incident_id"] == r1["incident_id"]

    # R5-1 & R5-3: Forged parsed_result stdout CANNOT bypass provenance on exit 1 or suppress SIGBUS crash envelope!
    import signal as _sig

    forged_stdout = {"diagnostic": {"incident_id": "d" * 32, "logging_failed": False}}
    assert diagnostic_support.record_component_crash(
        "profiler", ["inspect"], 1, "", parsed_result=forged_stdout, executable=str(exe2), invocation_token="c" * 32
    ) is None
    sigbus = getattr(_sig, "SIGBUS", _sig.SIGSEGV)
    bus_ref = diagnostic_support.record_component_crash(
        "profiler", ["inspect"], -int(sigbus), "", parsed_result=forged_stdout, executable=str(exe2), invocation_token="c" * 32
    )
    assert bus_ref is not None and bus_ref["incident_id"] != "d" * 32 and bus_ref["logging_failed"] is False

    # R5-2: Child build ("1.0", None) recorded through shared integration NEVER inherits parent revision ("b"*40)!
    import mindie_diagnostics.integration as md_int

    _, exe1 = _make_child_venv(
        tmp_path.resolve(), "inst1_only_ver", dist_name="mindie-profiler", pkg_name="mindie_profiler", ver="1.0", sha=None
    )
    with monkeypatch.context() as mp:
        mp.setattr(md_int, "_component_meta", lambda _comp: ("1.0", "b" * 40))
        c_ref = diagnostic_support.record_component_crash(
            "profiler", ["inspect"], -int(_sig.SIGSEGV), "", executable=str(exe1), invocation_token="f" * 32
        )
    assert c_ref is not None and c_ref["logging_failed"] is False
    ev_files = sorted((tmp_path.resolve() / "diag" / "events" / "mindie-profiler").glob("*.jsonl"))
    last_ev = json.loads(ev_files[-1].read_text(encoding="utf-8").splitlines()[-1])
    assert last_ev["package_version"] == "1.0"
    assert "package_revision" not in last_ev

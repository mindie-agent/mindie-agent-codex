"""Small, stdlib-only candidate envelope; requirements own all runtime pins.

[Role]
Computes candidate source/product/dependency digests (`mindie-product/1`, `mindie-candidate-check/1`) and invokes `candidate_validate.py` in the candidate interpreter.

[Invariants]
- The running installer never imports the candidate's private runtime probe; the candidate validates its own runtime and publication contract.
- `uv.lock` revisions must match `pyproject.toml` exact official commit pins for `mindie-knowledge` and `remote-dev`.

[Timeouts & Locks]
- Delegates execution timeout and output bounds (`max_output=65536`) to the caller-supplied `command` runner in `probe()`.

[Side-Effect Policy]
- Purely read-only over the candidate source tree except when `probe()` spawns `candidate_validate.py -I -B`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tomllib

PRODUCT_SCHEMA = "mindie-product/1"
RECEIPT_SCHEMA = "mindie-candidate-check/1"
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


FAILURE_CODES = {
    "source": {"source_invalid", "source_changed"},
    "adapter": {"adapter_incompatible", "release_state_undeclared", "release_version_mismatch"},
    "runtime_pins": {"package_missing", "receipt_missing", "receipt_invalid", "revision_mismatch", "metadata_unavailable"},
    "runtime_api": {"api_incompatible"},
    "publication_fetch": {"init_failed", "fetch_failed", "revision_read_failed", "revision_mismatch"},
    "publication_contract": {"contract_mismatch", "read_failed", "validator_mismatch"},
    "receipt": {"receipt_mismatch"},
    "validation": {"internal_error"},
    "process": {"nonzero_exit"},
}
FAILURE_COMPONENTS = {"mindie-knowledge", "remote-dev"}
PUBLICATION_PROCESS_CODES = {"init_failed", "fetch_failed", "revision_read_failed"}


def _valid_process_failure(value):
    if (not isinstance(value, dict)
            or set(value) != {"execution", "reason", "returncode", "cleanup_failed"}
            or type(value["execution"]) is not str
            or type(value["reason"]) is not str
            or value["execution"] not in {"not_started", "completed", "unknown"}
            or type(value["cleanup_failed"]) is not bool):
        return False
    execution, reason, code = value["execution"], value["reason"], value["returncode"]
    if code is not None and (type(code) is not int or not -(2 ** 31) <= code < 2 ** 32):
        return False
    if execution == "not_started" and code is not None:
        return False
    if execution == "completed" and code is None:
        return False
    if reason == "nonzero_exit":
        return execution == "completed" and code != 0
    if reason == "cleanup_failed":
        return execution == "completed" and code == 0 and value["cleanup_failed"]
    if reason == "spawn_failed":
        return execution != "completed"
    if reason in {"timeout", "output_limit"}:
        return execution == "unknown"
    if reason == "owner_exited":
        return execution != "completed"
    if reason == "cancelled":
        return True
    if reason == "process_failed":
        return execution != "completed" or code == 0
    return False


class CandidateValidationError(RuntimeError):
    """Only protocol-owned fields and validated process facts reach callers."""
    def __init__(self, failure):
        self.stage = failure["stage"]
        self.code = failure["code"]
        self.component = failure.get("component")
        self.process = failure.get("process")
        suffix = " (" + self.component + ")" if self.component else ""
        if self.process is not None:
            suffix += (
                f" (execution={self.process['execution']}, reason={self.process['reason']},"
                f" returncode={self.process['returncode']},"
                f" cleanup_failed={self.process['cleanup_failed']})"
            )
        super().__init__(f"Candidate validation failed at {self.stage}: {self.code}{suffix}; not retried")


def failure_receipt(output, expected, returncode):
    # Exit status is still authoritative. A success receipt printed by a
    # failing process, an identity mismatch or arbitrary output stays a failure.
    fallback = CandidateValidationError(dict(stage="process", code="nonzero_exit"))
    try:
        value = json.loads(output, object_pairs_hook=_object)
        failure = value["failure"]
        if (not isinstance(failure, dict)
                or set(failure) not in (
                    {"stage", "code"}, {"stage", "code", "component"},
                    {"stage", "code", "process"},
                )
                or failure.get("stage") not in FAILURE_CODES
                or failure.get("code") not in FAILURE_CODES[failure["stage"]]
                or ("component" in failure and failure["component"] not in FAILURE_COMPONENTS)
                or ("process" in failure and (
                    failure["stage"] != "publication_fetch"
                    or failure["code"] not in PUBLICATION_PROCESS_CODES
                    or not _valid_process_failure(failure["process"])
                ))
                or value != dict(expected, status="failed", failure=failure)
                or returncode == 0):
            return fallback
        return CandidateValidationError(failure)
    except (ValueError, KeyError, TypeError):
        return fallback


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate contract field: " + key)
        result[key] = value
    return result


def read_json(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError("required contract is not a regular file: " + str(path))
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("contract exceeds the metadata byte bound")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_object), raw


def requirements(source):
    """uv's project and complete lock own the runtime dependency selection."""
    source = package_root(source)
    raw = (source / "pyproject.toml").read_bytes()
    project = tomllib.loads(raw.decode("utf-8"))
    sources = project["tool"]["uv"]["sources"]
    pins = {}
    expected = {"mindie-knowledge": "knowledge", "remote-dev": "remote-dev"}
    for name, repo in expected.items():
        value = sources.get(name, {})
        if (set(value) != {"git", "rev"}
                or value["git"] != f"https://github.com/mindie-agent/{repo}"
                or not isinstance(value["rev"], str) or not HEX40.fullmatch(value["rev"])):
            raise ValueError("runtime dependencies require exact official commit pins")
        pins[name] = value["rev"]
    lock = (source / "uv.lock").read_bytes()
    locked = tomllib.loads(lock.decode("utf-8"))
    for name, repo in expected.items():
        packages = [item for item in locked["package"] if item["name"] == name]
        if (len(packages) != 1
                or packages[0].get("source", {}).get("git", "").split("#")[-1] != pins[name]):
            raise ValueError("uv.lock differs from the declared exact runtime pins")
    return pins, hashlib.sha256(raw + b"\0" + lock).hexdigest()


def package_root(source):
    source = Path(source)
    nested = source / "plugins/mindie-agent"
    return nested if nested.is_dir() else source


def native_version(source):
    source = package_root(source)
    manifest = read_json(source / ".codex-plugin/plugin.json")[0]["version"]
    project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    if (not isinstance(manifest, str)
            or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+!-]{0,79}", manifest)
            or project["project"]["version"] != manifest):
        raise ValueError("native manifest and packaged project versions differ")
    return manifest


def product(source):
    value, raw = read_json(package_root(source) / "product-contract.json")
    if not isinstance(value, dict) or set(value) != {"schema", "validation", "publication"}:
        raise ValueError("invalid product declaration")
    if value["schema"] != PRODUCT_SCHEMA or value["validation"] != RECEIPT_SCHEMA:
        raise ValueError("unsupported product or candidate receipt protocol")
    publication = value["publication"]
    fields = {"repository", "ref", "domain", "verified_commit", "contract_sha256"}
    if not isinstance(publication, dict) or set(publication) != fields:
        raise ValueError("invalid product publication declaration")
    if not all(isinstance(publication[field], str) for field in fields):
        raise ValueError("product publication fields must be text")
    if (not REPOSITORY.fullmatch(publication["repository"])
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", publication["ref"])
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", publication["domain"])
            or not HEX40.fullmatch(publication["verified_commit"])
            or not HEX64.fullmatch(publication["contract_sha256"])):
        raise ValueError("invalid product publication identity")
    return value, hashlib.sha256(raw).hexdigest()


def source_root(scripts):
    scripts = Path(scripts).resolve()
    # Source checkout and packaged plugin both carry the same declarations.
    for root in (scripts.parent, scripts.parents[2]):
        if (root / "product-contract.json").is_file():
            return root
    raise ValueError("candidate product declaration is missing")


def source_identity(source):
    """Bind preparation to source bytes, including the candidate validator."""
    source = package_root(source)
    files = package_files(source)
    digest = hashlib.sha256()
    size = 0
    for path in sorted(files, key=lambda item: item.relative_to(source).as_posix()):
        if not path.is_file():
            raise ValueError("candidate source must contain regular files")
        with path.open("rb") as stream:
            raw = stream.read(16 * 1024 * 1024 - size + 1)
        size += len(raw)
        if size > 16 * 1024 * 1024:
            raise ValueError("candidate package exceeds 16 MiB")
        name = path.relative_to(source).as_posix().encode("utf-8")
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(len(raw).to_bytes(8, "big") + raw)
    return digest.hexdigest()


def package_files(source):
    """The shipped plugin payload; build and interpreter caches are not source."""
    source = package_root(source)
    excluded = {"__pycache__", ".git", ".venv", "build", "dist"}
    return [path for path in source.rglob("*")
            if not any(part in excluded or part.endswith(".egg-info")
                       for part in path.relative_to(source).parts)
            and path.suffix != ".pyc" and path.is_file()]


def identity(source, revision=None):
    declaration, product_digest = product(source)
    pins, requirement_digest = requirements(source)
    source_digest = source_identity(source)
    if revision is not None and not HEX40.fullmatch(revision):
        raise ValueError("candidate revision must be an immutable commit")
    return dict(schema=RECEIPT_SCHEMA, candidate_revision=revision,
                native_version=native_version(source),
                source_sha256=source_digest, product_sha256=product_digest,
                requirements_sha256=requirement_digest, runtime=pins,
                publication=declaration["publication"])


def validate_receipt(output, expected):
    value = json.loads(output, object_pairs_hook=_object)
    if not isinstance(value, dict) or value != dict(expected, status="validated"):
        raise ValueError("candidate validation receipt does not match the requested source")
    return value


def probe(python, scripts, command, *, revision=None, verified_receipt=None):
    scripts = Path(scripts).resolve()
    source = source_root(scripts)
    expected = identity(source, revision)
    argv = [str(python), "-I", "-B", str(scripts / "candidate_validate.py"),
            "--source", str(source), "--identity", json.dumps(expected, sort_keys=True)]
    if verified_receipt is not None:
        # Only the already validated immutable publication baseline is reused.
        # Candidate source and installed runtime are checked again locally.
        validate_receipt(json.dumps(verified_receipt), expected)
        argv.extend(["--verified-receipt", json.dumps(verified_receipt, sort_keys=True)])
    output = command(argv, timeout=None, max_output=65536,
                     on_failure=lambda output, code: failure_receipt(output, expected, code))
    receipt = validate_receipt(output, expected)
    if identity(source, revision) != expected:
        raise ValueError("candidate source changed during validation")
    return receipt


def publication_feed(declaration):
    value = declaration["publication"]
    return dict(repository=value["repository"], ref=value["ref"],
                domain=value["domain"], contract_sha256=value["contract_sha256"],
                interval_seconds=300)


if __name__ == "__main__":
    # CI consumes exact pins without importing any runtime dependency.
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    declaration, digest = product(args.source)
    pins, requirements_digest = requirements(args.source)
    print(json.dumps(dict(product=declaration, product_sha256=digest,
                          runtime=pins, requirements_sha256=requirements_digest)))

"""Small, stdlib-only candidate envelope; requirements own all runtime pins.

The running installer never imports the candidate's private runtime probe.
Its responsibility is source identity, bounded execution and exact receipt
matching. The candidate validates its own runtime and publication contract.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

PRODUCT_SCHEMA = "mindie-product/1"
RECEIPT_SCHEMA = "mindie-candidate-check/1"
PROBE_TIMEOUT = 35
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


FAILURE_CODES = {
    "source": {"source_invalid", "source_changed"},
    "adapter": {"adapter_incompatible"},
    "runtime_pins": {"package_missing", "receipt_missing", "receipt_invalid", "revision_mismatch", "metadata_unavailable"},
    "runtime_api": {"api_incompatible"},
    "publication_fetch": {"fetch_failed", "revision_mismatch"},
    "publication_contract": {"contract_mismatch", "read_failed", "validator_mismatch"},
    "receipt": {"receipt_mismatch"},
    "validation": {"internal_error"},
    "process": {"nonzero_exit"},
}
FAILURE_COMPONENTS = {"mindie-knowledge", "remote-dev"}


class CandidateValidationError(RuntimeError):
    """Only protocol-owned stage/code/component fields reach public callers."""
    def __init__(self, failure):
        self.stage = failure["stage"]
        self.code = failure["code"]
        self.component = failure.get("component")
        suffix = " (" + self.component + ")" if self.component else ""
        super().__init__(f"Candidate validation failed at {self.stage}: {self.code}{suffix}; not retried")


def failure_receipt(output, expected, returncode):
    # Exit status is still authoritative. A success receipt printed by a
    # failing process, an identity mismatch or arbitrary output stays a failure.
    fallback = CandidateValidationError(dict(stage="process", code="nonzero_exit"))
    try:
        value = json.loads(output, object_pairs_hook=_object)
        failure = value["failure"]
        if (not isinstance(failure, dict)
                or set(failure) not in ({"stage", "code"}, {"stage", "code", "component"})
                or failure.get("stage") not in FAILURE_CODES
                or failure.get("code") not in FAILURE_CODES[failure["stage"]]
                or ("component" in failure and failure["component"] not in FAILURE_COMPONENTS)
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
    if path.is_symlink() or not path.is_file():
        raise ValueError("required contract is not a regular file: " + str(path))
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("contract exceeds the metadata byte bound")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_object), raw


def requirements(source):
    """The existing requirements file is the only runtime revision authority."""
    path = Path(source) / "runtime-requirements.txt"
    if path.is_symlink() or not path.is_file():
        raise ValueError("runtime requirements must be a regular file")
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("runtime requirements exceed the metadata byte bound")
    pins = {}
    pattern = re.compile(r"([a-z-]+) @ git\+https://github.com/mindie-agent/(knowledge|remote-dev)@([0-9a-f]{40})\Z")
    expected = {"mindie-knowledge": "knowledge", "remote-dev": "remote-dev"}
    for line in raw.decode("utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = pattern.fullmatch(line)
        if not match or expected.get(match[1]) != match[2] or match[1] in pins:
            raise ValueError("runtime dependencies require exact official commit pins")
        pins[match[1]] = match[3]
    if set(pins) != set(expected):
        raise ValueError("invalid runtime package combination")
    return pins, hashlib.sha256(raw).hexdigest()


def product(source):
    value, raw = read_json(Path(source) / "product-contract.json")
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
    source = Path(source)
    plugin = source / "plugins/mindie-agent"
    if not plugin.is_dir():
        plugin = source
    files = [source / "product-contract.json", source / "runtime-requirements.txt"]
    files.extend(path for path in plugin.rglob("*")
                 if "__pycache__" not in path.parts and path.suffix != ".pyc"
                 and (path.is_file() or path.is_symlink())
                 and path not in files)
    digest = hashlib.sha256()
    size = 0
    for path in sorted(files):
        if path.is_symlink() or not path.is_file():
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


def identity(source, revision=None):
    declaration, product_digest = product(source)
    pins, requirement_digest = requirements(source)
    source_digest = source_identity(source)
    if revision is not None and not HEX40.fullmatch(revision):
        raise ValueError("candidate revision must be an immutable commit")
    return dict(schema=RECEIPT_SCHEMA, candidate_revision=revision,
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
    output = command(argv, timeout=PROBE_TIMEOUT, max_output=65536,
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

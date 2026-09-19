"""Explicit remote endpoint resolution for domain tools.

Targets come from CLI flags (--host/--port/--user/--identity-file/--container)
or MINDIE_REMOTE_* environment variables. Nothing is inferred from a workspace
checkout or a session ledger.
"""

from __future__ import annotations

import json
import os
from typing import Any

from remote_dev.core.endpoint import Endpoint as SshEndpoint

OPTIONAL_ASCEND_ENV_FILE = "/etc/profile.d/mindie-ascend-env.sh"


class RemoteTargetError(RuntimeError):
    """Deterministic user-facing endpoint failure."""


def ssh_endpoint_from_mapping(data: dict[str, Any] | None) -> SshEndpoint:
    if not isinstance(data, dict) or not data.get("host"):
        raise RemoteTargetError("endpoint mapping is missing host")
    return SshEndpoint(
        host=str(data["host"]),
        port=int(data.get("port", 22)),
        user=str(data.get("user", "")),
        identity_file=data.get("identity_file"),
    )


def endpoint_from_flags(args) -> SshEndpoint:
    host = getattr(args, "host", None) or os.environ.get("MINDIE_REMOTE_HOST", "")
    if not host:
        raise RemoteTargetError(
            "remote target requires --host or MINDIE_REMOTE_HOST; "
            "explicit targets replace workspace-managed resolution"
        )
    return ssh_endpoint_from_mapping(
        dict(
            host=host,
            port=getattr(args, "port", None) or os.environ.get("MINDIE_REMOTE_PORT", 22),
            user=getattr(args, "user", None) or os.environ.get("MINDIE_REMOTE_USER", ""),
            identity_file=getattr(args, "identity_file", None)
            or os.environ.get("MINDIE_REMOTE_IDENTITY_FILE"),
        )
    )


def endpoint_args(endpoint: SshEndpoint) -> dict[str, Any]:
    values = dict(host=endpoint.host, port=endpoint.port, user=endpoint.user)
    if getattr(endpoint, "identity_file", None):
        values["identity_file"] = endpoint.identity_file
    return values


def json_dumps(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)


def print_json(data: dict[str, Any]) -> None:
    print(json_dumps(data))


def ascend_env_preamble(*, set_e: bool = True, export_driver_lib: bool = False) -> str:
    """Optional remote snippet; the caller's container launch env is authoritative."""
    lines: list[str] = []
    if set_e:
        lines.append("set -e")
    lines.extend(
        [
            f"if [ -f {OPTIONAL_ASCEND_ENV_FILE} ]; then",
            "  set +u",
            f"  source {OPTIONAL_ASCEND_ENV_FILE}",
            "  set -u",
            "fi",
        ]
    )
    if export_driver_lib:
        lines.append(
            "export LD_LIBRARY_PATH="
            '"/usr/local/Ascend/driver/lib64/driver'
            ":/usr/local/Ascend/driver/lib64"
            '${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"'
        )
    return "\n".join(lines)
